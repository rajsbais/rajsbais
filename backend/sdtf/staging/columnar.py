"""Columnar staging: Parquet files under <staging_dir>/<run_id>/<table>/<partition>.parquet.

The directory is an object-storage mount (PVC, S3/GCS via a CSI driver or fsspec, planned); files are written
once by extraction and rewritten per (table, partition) by transformation and load. Payloads are stored as JSON
strings in dedicated columns next to key/status columns, so external tooling (Spark, DuckDB, pandas) can read
staging directly. Within a run, (table, record_key) is unique: a later partition never overwrites an earlier copy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from collections import defaultdict
from typing import Iterable, Iterator

from .base import StagedRow

SCHEMA_COLUMNS = ["record_key", "source_payload", "target_payload", "target_key", "lineage", "load_status"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", name)


class ColumnarStaging:
    name = "columnar"

    def __init__(self, base_dir: str):
        try:
            import pyarrow  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("columnar staging requires pyarrow (pip install 'sdtf[columnar]')") from e
        self.base = base_dir

    # ------------------------------------------------------------------ paths
    def _run_dir(self, run_id: str) -> str:
        return os.path.join(self.base, run_id)

    def _file(self, run_id: str, table: str, partition: str) -> str:
        return os.path.join(self._run_dir(run_id), _safe(table), _safe(partition) + ".parquet")

    def _files(self, run_id: str, table: str | None = None) -> list[tuple[str, str, str]]:
        out = []
        rd = self._run_dir(run_id)
        if not os.path.isdir(rd):
            return out
        for t in sorted(os.listdir(rd)):
            if table and t != _safe(table):
                continue
            td = os.path.join(rd, t)
            if not os.path.isdir(td):
                continue
            for f in sorted(os.listdir(td)):
                if f.endswith(".parquet"):
                    out.append((t, f[: -len(".parquet")], os.path.join(td, f)))
        return out

    # ------------------------------------------------------------------ io
    def _write(self, path: str, rows: list[StagedRow]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        os.makedirs(os.path.dirname(path), exist_ok=True)
        table = pa.table({
            "record_key": pa.array([r.record_key for r in rows], pa.string()),
            "source_payload": pa.array([json.dumps(r.source_payload, default=str) for r in rows], pa.string()),
            "target_payload": pa.array([json.dumps(r.target_payload, default=str) if r.target_payload is not None else None for r in rows], pa.string()),
            "target_key": pa.array([r.target_key for r in rows], pa.string()),
            "lineage": pa.array([json.dumps(r.lineage) for r in rows], pa.string()),
            "load_status": pa.array([r.load_status for r in rows], pa.string()),
        })
        tmp = path + ".tmp"
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)

    def _read(self, path: str, table: str, partition: str) -> list[StagedRow]:
        import pyarrow.parquet as pq

        t = pq.read_table(path).to_pydict()
        out = []
        for i in range(len(t["record_key"])):
            tp = t["target_payload"][i]
            out.append(StagedRow(partition, table, t["record_key"][i], json.loads(t["source_payload"][i]), json.loads(tp) if tp else None, t["target_key"][i], json.loads(t["lineage"][i] or "[]"), t["load_status"][i]))
        return out

    # ------------------------------------------------------------------ contract
    def write_partition(self, run_id: str, partition: str, rows: Iterable[StagedRow]) -> int:
        by_table: dict[str, list[StagedRow]] = defaultdict(list)
        for r in rows:
            by_table[r.table_name].append(r)
        n = 0
        for table, trows in by_table.items():
            existing = self.keys(run_id, table)
            seen = set()
            fresh = []
            for r in trows:
                if r.record_key in existing or r.record_key in seen:
                    continue
                seen.add(r.record_key)
                fresh.append(r)
            if fresh:
                self._write(self._file(run_id, table, partition), fresh)
                n += len(fresh)
        return n

    def iter_records(self, run_id: str, table: str | None = None, status: str | None = None, partition: str | None = None) -> Iterator[StagedRow]:
        for t, p, path in self._files(run_id, table):
            if partition and p != _safe(partition):
                continue
            for r in self._read(path, t, p):
                if status and r.load_status != status:
                    continue
                yield r

    def update_records(self, run_id: str, rows: Iterable[StagedRow]) -> int:
        updates: dict[tuple[str, str], dict[str, StagedRow]] = defaultdict(dict)
        for r in rows:
            updates[(r.table_name, r.partition)][r.record_key] = r
        n = 0
        for (table, partition), m in updates.items():
            path = self._file(run_id, table, partition)
            current = self._read(path, table, partition)
            merged = [m.get(c.record_key, c) for c in current]
            self._write(path, merged)
            n += len(m)
        return n

    def counts(self, run_id: str) -> list[dict]:
        c: dict[tuple[str, str], int] = defaultdict(int)
        for r in self.iter_records(run_id):
            c[(r.table_name, r.load_status)] += 1
        return [{"table": t, "status": s, "count": n} for (t, s), n in sorted(c.items())]

    def samples(self, run_id: str, table: str | None, limit: int) -> list[StagedRow]:
        out = []
        for r in self.iter_records(run_id, table):
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def keys(self, run_id: str, table: str) -> set[str]:
        import pyarrow.parquet as pq

        ks: set[str] = set()
        for _, _, path in self._files(run_id, table):
            ks.update(pq.read_table(path, columns=["record_key"]).column("record_key").to_pylist())
        return ks

    def drop_run(self, run_id: str) -> None:
        shutil.rmtree(self._run_dir(run_id), ignore_errors=True)

    def footprint(self, run_id: str) -> dict:
        files = self._files(run_id)
        return {"files": len(files), "bytes": sum(os.path.getsize(p) for _, _, p in files)}

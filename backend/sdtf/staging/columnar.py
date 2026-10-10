"""Columnar staging: Parquet files under <staging_dir>/<run_id>/<table>/<partition>.parquet.

`staging_dir` is any fsspec URL or local path: a mounted volume (`/app/data/staging`), `file://…`, `memory://…`
(tests), `s3://bucket/prefix` (needs `s3fs`) or `gs://bucket/prefix` (needs `gcsfs`). Credentials come from the
environment, instance roles or workload identity, never from the platform database; filesystem options such as an
S3-compatible `endpoint_url` are passed through `SDTF_STAGING_FS_OPTIONS` (JSON).

Files are written once by extraction and rewritten per (table, partition) by transformation and load. On object
stores a PUT is atomic, so files are written directly; on local filesystems a temp file is renamed into place.
Payloads are JSON strings in dedicated columns next to key/status columns, so external tooling (Spark, DuckDB,
pandas) can read staging directly. Within a run, (table, record_key) is unique: a later partition never overwrites
an earlier copy. Every file has a sidecar key-range index (`staging/index.py`) so membership checks and counts avoid
reading Parquet columns; `self.stats` records how many files the index pruned.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict
from typing import Iterable, Iterator

from .. import observability as obs
from .base import StagedRow
from .index import FileIndex, index_path

SCHEMA_COLUMNS = ["record_key", "source_payload", "target_payload", "target_key", "lineage", "load_status"]
OBJECT_STORE_PROTOCOLS = {"s3", "s3a", "gs", "gcs", "abfs", "az", "adl", "oci", "memory"}
_DRIVER_HINT = {"s3": "s3fs (pip install 'sdtf[s3]')", "s3a": "s3fs (pip install 'sdtf[s3]')", "gs": "gcsfs (pip install 'sdtf[gcs]')", "gcs": "gcsfs (pip install 'sdtf[gcs]')", "abfs": "adlfs", "az": "adlfs"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", name)


class ColumnarStaging:
    name = "columnar"

    def __init__(self, base_dir: str, fs_options: dict | None = None):
        try:
            import pyarrow  # noqa: F401
            from fsspec.core import url_to_fs
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("columnar staging requires pyarrow and fsspec (pip install 'sdtf[columnar]')") from e
        if fs_options is None:
            from .. import config

            fs_options = config.settings.staging_fs_options
        try:
            self.fs, self.root = url_to_fs(base_dir, **(fs_options or {}))
        except ImportError as e:
            proto = base_dir.split("://", 1)[0] if "://" in base_dir else "file"
            raise RuntimeError(f"staging location {base_dir!r} needs the {_DRIVER_HINT.get(proto, proto)} driver: {e}") from e
        self.base = base_dir
        proto = self.fs.protocol if isinstance(self.fs.protocol, str) else self.fs.protocol[0]
        self.protocol = proto
        self.object_store = proto in OBJECT_STORE_PROTOCOLS
        self.root = self.root.rstrip("/")
        self._index_cache: dict[tuple[str, int], FileIndex | None] = {}
        self.stats = {"files_listed": 0, "files_pruned": 0, "files_scanned": 0, "index_reads": 0}

    # ------------------------------------------------------------------ paths
    def _run_dir(self, run_id: str) -> str:
        return posixpath.join(self.root, run_id)

    def _file(self, run_id: str, table: str, partition: str) -> str:
        return posixpath.join(self._run_dir(run_id), _safe(table), _safe(partition) + ".parquet")

    def _files(self, run_id: str, table: str | None = None) -> list[tuple[str, str, str]]:
        rd = self._run_dir(run_id)
        if not self.fs.exists(rd):
            return []
        prefix = posixpath.join(rd, _safe(table)) if table else rd
        if table and not self.fs.exists(prefix):
            return []
        out = []
        listing = self.fs.find(prefix, detail=True)
        self._sizes = {k: v for k, v in getattr(self, "_sizes", {}).items() if not k.startswith(prefix)}  # refresh this prefix
        for path, info in sorted(listing.items()):
            self._sizes[path] = info.get("size", 0) or 0
            if not path.endswith(".parquet"):
                continue
            rel = path[len(rd) :].strip("/") if path.startswith(rd) else path.split(run_id + "/", 1)[-1]
            parts = rel.split("/")
            if len(parts) != 2:
                continue
            out.append((parts[0], parts[1][: -len(".parquet")], path))
        return out

    # ------------------------------------------------------------------ index
    def _index_for(self, path: str) -> FileIndex | None:
        """Sidecar index of a Parquet file (cached by path + sidecar size); None when the file has no sidecar."""
        ip = index_path(path)
        size = self._sizes.get(ip) if hasattr(self, "_sizes") else None
        if size is None:
            if not self.fs.exists(ip):
                return None
            size = self.fs.size(ip)
        key = (ip, size)
        if key not in self._index_cache:
            try:
                with self.fs.open(ip, "r") as fh:
                    self._index_cache[key] = FileIndex.from_json(fh.read())
            except FileNotFoundError:  # sidecar vanished between listing and read: scan the file instead
                self._sizes.pop(ip, None)
                return None
            self.stats["index_reads"] += 1
        return self._index_cache[key]

    def _write_index(self, path: str, rows: list[StagedRow]) -> None:
        idx = FileIndex.build((r.record_key for r in rows), (r.load_status for r in rows))
        ip = index_path(path)
        data = idx.to_json()
        if self.object_store:
            with self.fs.open(ip, "w") as fh:
                fh.write(data)
        else:
            with self.fs.open(ip + ".tmp", "w") as fh:
                fh.write(data)
            self.fs.mv(ip + ".tmp", ip)

    def contains(self, run_id: str, table: str, keys) -> set[str]:
        """Which of `keys` already exist for the table. Prunes files by key range and Bloom filter."""
        wanted = set(keys)
        if not wanted:
            return set()
        before = dict(self.stats)
        try:
            return self._contains(run_id, table, wanted)
        finally:
            obs.counter("sdtf.staging.files_pruned", self.stats["files_pruned"] - before["files_pruned"], table=table)
            obs.counter("sdtf.staging.files_scanned", self.stats["files_scanned"] - before["files_scanned"], table=table)

    def _contains(self, run_id: str, table: str, wanted: set[str]) -> set[str]:
        lo, hi = min(wanted), max(wanted)
        found: set[str] = set()
        import pyarrow.parquet as pq

        for _, _, path in self._files(run_id, table):
            self.stats["files_listed"] += 1
            idx = self._index_for(path)
            if idx is not None:
                if not idx.overlaps(lo, hi) or not any(idx.may_contain(k) for k in wanted):
                    self.stats["files_pruned"] += 1
                    continue
            self.stats["files_scanned"] += 1
            found.update(set(pq.read_table(path, filesystem=self.fs, columns=["record_key"]).column("record_key").to_pylist()) & wanted)
            if len(found) == len(wanted):
                break
        return found

    # ------------------------------------------------------------------ io
    def _write(self, path: str, rows: list[StagedRow]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.fs.makedirs(posixpath.dirname(path), exist_ok=True)
        table = pa.table({
            "record_key": pa.array([r.record_key for r in rows], pa.string()),
            "source_payload": pa.array([json.dumps(r.source_payload, default=str) for r in rows], pa.string()),
            "target_payload": pa.array([json.dumps(r.target_payload, default=str) if r.target_payload is not None else None for r in rows], pa.string()),
            "target_key": pa.array([r.target_key for r in rows], pa.string()),
            "lineage": pa.array([json.dumps(r.lineage) for r in rows], pa.string()),
            "load_status": pa.array([r.load_status for r in rows], pa.string()),
        })
        if self.object_store:  # a single PUT is atomic on object stores
            pq.write_table(table, path, filesystem=self.fs, compression="zstd")
        else:
            tmp = path + ".tmp"
            pq.write_table(table, tmp, filesystem=self.fs, compression="zstd")
            self.fs.mv(tmp, path)
        self._write_index(path, rows)

    def _read(self, path: str, table: str, partition: str) -> list[StagedRow]:
        import pyarrow.parquet as pq

        t = pq.read_table(path, filesystem=self.fs).to_pydict()
        out = []
        for i in range(len(t["record_key"])):
            tp = t["target_payload"][i]
            out.append(StagedRow(partition, table, t["record_key"][i], json.loads(t["source_payload"][i]), json.loads(tp) if tp else None, t["target_key"][i], json.loads(t["lineage"][i] or "[]"), t["load_status"][i]))
        return out

    # ------------------------------------------------------------------ contract
    def write_partition(self, run_id: str, partition: str, rows: Iterable[StagedRow]) -> int:
        with obs.timed("sdtf.staging.write_partition", run_id=run_id, partition=partition, backend=self.name, protocol=self.protocol) as s:
            n = self._write_partition(run_id, partition, rows)
            s.set_attribute("records", n)
            obs.counter("sdtf.staging.records_written", n, backend=self.name)
            return n

    def _write_partition(self, run_id: str, partition: str, rows: Iterable[StagedRow]) -> int:
        by_table: dict[str, list[StagedRow]] = defaultdict(list)
        for r in rows:
            by_table[r.table_name].append(r)
        n = 0
        for table, trows in by_table.items():
            existing = self.contains(run_id, table, [r.record_key for r in trows])
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
        import pyarrow.parquet as pq

        c: dict[tuple[str, str], int] = defaultdict(int)
        for t, _, path in self._files(run_id):
            idx = self._index_for(path)
            if idx is not None:
                for st, n in idx.status_counts.items():
                    c[(t, st)] += n
                continue
            for st in pq.read_table(path, filesystem=self.fs, columns=["load_status"]).column("load_status").to_pylist():
                c[(t, st)] += 1
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
            ks.update(pq.read_table(path, filesystem=self.fs, columns=["record_key"]).column("record_key").to_pylist())
        return ks

    def drop_run(self, run_id: str) -> None:
        rd = self._run_dir(run_id)
        if self.fs.exists(rd):
            self.fs.rm(rd, recursive=True)

    def footprint(self, run_id: str) -> dict:
        files = self._files(run_id)
        indexed = sum(1 for _, _, p in files if index_path(p) in self._sizes)
        return {"files": len(files), "bytes": sum(self._sizes.get(p, 0) for _, _, p in files), "index_files": indexed, "index_bytes": sum(self._sizes.get(index_path(p), 0) for _, _, p in files), "location": self.base, "protocol": self.protocol, "stats": dict(self.stats)}

"""Selective extraction driven by the approved manifest.

Partitions = (business object type, owning company code). Each partition is extracted by a worker, written to
the staging table, and checkpointed, so a restarted run skips completed partitions. Source-load throttling is
modelled as a records-per-second budget per worker.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.store import RecordStore
from ..catalog.tables import TABLES, record_key
from ..graph.service import split_node
from ..staging import StagedRow, get_backend
from .adapters import ExtractedRecord, Partition

TRANSFER_CLASSES = ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")


@dataclass
class ExtractionPlan:
    partitions: list[Partition]
    objects_by_partition: dict[str, list[str]]
    snapshot_id: str


class SyntheticStoreExtractor:
    name = "SYNTHETIC"
    status = "SIMULATED"

    def __init__(self, store: RecordStore, classification: dict[str, dict], scope_ccs: set[str], max_rows_per_second: int | None = None):
        self.store = store
        self.classification = classification
        self.scope_ccs = scope_ccs
        self.max_rps = max_rows_per_second
        self._plan: ExtractionPlan | None = None
        self.retained_rows = 0
        self.retained_headers: set[str] = set()
        self.reference_stubs: set[str] = set()

    def snapshot(self) -> str:
        """Consistency token: hash of per-table row counts. A production adapter would obtain a database
        snapshot / consistent read token from the source instead."""
        h = hashlib.sha256()
        for t in self.store.tables():
            h.update(f"{t}:{self.store.count(t)};".encode())
        return "snap-" + h.hexdigest()[:16]

    def plan(self) -> ExtractionPlan:
        if self._plan:
            return self._plan
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for nid, c in self.classification.items():
            t = c["type"]
            if t not in BUSINESS_OBJECTS:
                continue
            is_stub = c["classification"] == "REFERENCE_ONLY" and BUSINESS_OBJECTS[t].kind == "MASTER" and BUSINESS_OBJECTS[t].org_scope == "CLIENT"
            if c["classification"] not in TRANSFER_CLASSES and not is_stub:
                continue
            owner = (c["company_codes"] or ["*"])[0]
            groups[(t, owner)].append(nid)
        parts, objs = [], {}
        for (t, owner), nodes in sorted(groups.items()):
            pid = f"{t}@{owner}"
            parts.append(Partition(id=pid, table=BUSINESS_OBJECTS[t].header_table, object_type=t, predicate={"company_code": owner}, est_rows=len(nodes)))
            objs[pid] = sorted(nodes)
        self._plan = ExtractionPlan(parts, objs, self.snapshot())
        return self._plan

    def partitions(self) -> list[Partition]:
        return self.plan().partitions

    def _rows_for_object(self, bo_id: str, key: str, stub: bool = False) -> Iterable[tuple[str, dict]]:
        bo = BUSINESS_OBJECTS[bo_id]
        header = self.store.by_key(bo.header_table, key)
        if header is None:
            return
        yield bo.header_table, header
        if stub:  # reference-only master data: general data only, no organisational views
            return
        hk = {k: header[k] for k in bo.key_fields}
        for it in bo.item_tables:
            td = TABLES.get(it)
            if td is None:
                continue
            common = [k for k in bo.key_fields if k in td.fields]
            if not common:
                continue
            first = common[0]
            for row in self.store.lookup(it, first, hk[first]):
                if not all(str(row.get(k)) == str(hk[k]) for k in common):
                    continue
                # company-code / plant views of client-level master data: only views of carved-out org units move
                if it in ("KNB1", "LFB1") and row.get("BUKRS") not in self.scope_ccs:
                    continue
                if it in ("MARC", "MBEW", "MARD"):
                    k = self.store.get("T001K", BWKEY=row.get("WERKS") or row.get("BWKEY"))
                    if not k or k["BUKRS"] not in self.scope_ccs:
                        continue
                yield it, row

    def extract(self, partition: Partition) -> Iterable[ExtractedRecord]:
        plan = self.plan()
        bo_id = partition.object_type
        started = time.monotonic()
        n = 0
        for nid in plan.objects_by_partition.get(partition.id, []):
            _, key = split_node(nid)
            c = self.classification[nid]
            stub = c["classification"] == "REFERENCE_ONLY"
            partial = c["classification"] == "PARTIALLY_TRANSFERRED"
            bo = BUSINESS_OBJECTS[bo_id]
            for table, row in self._rows_for_object(bo_id, key, stub=stub):
                td = TABLES.get(table)
                # partially transferred cross-company documents: only the carved-out company code's side moves;
                # rows owned by retained company codes stay in the source (counted by the caller via node tags)
                if partial and td and td.org_field and td.org_field.startswith("BUKRS") and row.get(td.org_field) and row.get(td.org_field) not in self.scope_ccs:
                    self.retained_rows += 1
                    if table == bo.header_table:
                        self.retained_headers.add(nid)
                        break
                    continue
                if partial and td and td.org_field in ("WERKS", "DWERK"):
                    k = self.store.get("T001K", BWKEY=row.get(td.org_field))
                    if k and k["BUKRS"] not in self.scope_ccs:
                        self.retained_rows += 1
                        if table == bo.header_table:
                            self.retained_headers.add(nid)
                            break
                        continue
                if stub:
                    self.reference_stubs.add(nid)
                n += 1
                if self.max_rps:
                    elapsed = time.monotonic() - started
                    if n / max(elapsed, 1e-6) > self.max_rps:
                        time.sleep(n / self.max_rps - elapsed)
                yield ExtractedRecord(table=table, key=record_key(table, row), payload=row, object_node=nid)


def extract_partition(extractor: SyntheticStoreExtractor, partition: Partition, run_id: str, backend) -> int:
    """Extract one partition into the staging backend. Idempotent: duplicate (table, key) rows are ignored."""
    seen = set()
    rows = []
    for r in extractor.extract(partition):
        if (r.table, r.key) in seen:
            continue
        seen.add((r.table, r.key))
        rows.append(StagedRow(partition.id, r.table, r.key, r.payload))
    return backend.write_partition(run_id, partition.id, rows)


def run_extraction(session: Session, run_id: str, extractor: SyntheticStoreExtractor, checkpoint: dict, workers: int = 4, backend=None) -> dict:
    """Inline execution: extract all partitions not yet marked DONE in checkpoint with a thread pool.
    Distributed execution uses the same `extract_partition` from separate worker processes (runtime/worker.py)."""
    backend = backend or get_backend(session=session)
    plan = extractor.plan()
    done = set(checkpoint.get("partitions_done", []))
    todo = [p for p in plan.partitions if p.id not in done]
    metrics = {"partitions_total": len(plan.partitions), "partitions_skipped": len(done), "records": 0, "by_partition": {}}
    t0 = time.monotonic()
    lock = threading.Lock()

    def work(p: Partition):
        seen = set()
        rows = []
        for r in extractor.extract(p):
            if (r.table, r.key) in seen:
                continue
            seen.add((r.table, r.key))
            rows.append(StagedRow(p.id, r.table, r.key, r.payload))
        return p, rows

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for p, rows in ex.map(work, todo):
            with lock:  # staging writes are serialised; extraction itself ran in parallel
                n = backend.write_partition(run_id, p.id, rows)
            metrics["records"] += n
            metrics["by_partition"][p.id] = n
            done.add(p.id)
            checkpoint["partitions_done"] = sorted(done)
            session.flush()
    metrics["by_table"] = {c["table"]: c["count"] for c in backend.counts(run_id)} if todo else {}
    metrics["reference_stubs"] = len(extractor.reference_stubs)
    metrics["partial_rows_retained"] = extractor.retained_rows
    metrics["partial_headers_retained"] = len(extractor.retained_headers)
    metrics["duration_s"] = round(time.monotonic() - t0, 3)
    metrics["records_per_second"] = round(metrics["records"] / metrics["duration_s"], 1) if metrics["duration_s"] else None
    metrics["snapshot_id"] = plan.snapshot_id
    metrics["staging_backend"] = backend.name
    return metrics

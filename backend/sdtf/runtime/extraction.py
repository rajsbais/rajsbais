"""Selective extraction driven by the approved manifest.

Partitions = (business object type, owning company code). Each partition is extracted by a worker, written to
the staging table, and checkpointed, so a restarted run skips completed partitions. Source-load throttling is
modelled as a records-per-second budget per worker.
"""
from __future__ import annotations

import hashlib
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
from ..models import StagedRecord
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


def run_extraction(session: Session, run_id: str, extractor: SyntheticStoreExtractor, checkpoint: dict, workers: int = 4, batch: int = 1000) -> dict:
    """Extract all partitions not yet marked DONE in checkpoint. Returns updated checkpoint + metrics.

    Workers extract in parallel from the in-memory store; writes are serialised into the session (SQLite).
    """
    plan = extractor.plan()
    done = set(checkpoint.get("partitions_done", []))
    todo = [p for p in plan.partitions if p.id not in done]
    metrics = {"partitions_total": len(plan.partitions), "partitions_skipped": len(done), "records": 0, "by_table": defaultdict(int), "by_partition": {}}
    t0 = time.monotonic()

    def work(p: Partition):
        return p, list(extractor.extract(p))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for p, recs in ex.map(work, todo):
            seen = set()
            buf = []
            for r in recs:
                if (r.table, r.key) in seen:
                    continue
                seen.add((r.table, r.key))
                buf.append({"run_id": run_id, "partition": p.id, "table_name": r.table, "record_key": r.key, "source_payload": r.payload, "target_payload": None, "target_key": None, "lineage": [], "load_status": "STAGED"})
                metrics["by_table"][r.table] += 1
            # a record can belong to several objects (e.g. shared material document); keep first occurrence
            existing = {(t, k) for t, k in session.query(StagedRecord.table_name, StagedRecord.record_key).filter(StagedRecord.run_id == run_id, StagedRecord.table_name.in_({b["table_name"] for b in buf}) if buf else False)}
            buf = [b for b in buf if (b["table_name"], b["record_key"]) not in existing]
            for i in range(0, len(buf), batch):
                session.execute(StagedRecord.__table__.insert(), buf[i : i + batch])
            metrics["records"] += len(buf)
            metrics["by_partition"][p.id] = len(buf)
            done.add(p.id)
            checkpoint["partitions_done"] = sorted(done)
            session.flush()
    metrics["by_table"] = dict(metrics["by_table"])
    metrics["reference_stubs"] = len(extractor.reference_stubs)
    metrics["partial_rows_retained"] = extractor.retained_rows
    metrics["partial_headers_retained"] = len(extractor.retained_headers)
    metrics["duration_s"] = round(time.monotonic() - t0, 3)
    metrics["records_per_second"] = round(metrics["records"] / metrics["duration_s"], 1) if metrics["duration_s"] else None
    metrics["snapshot_id"] = plan.snapshot_id
    return metrics

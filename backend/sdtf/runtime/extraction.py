"""Selective extraction driven by the approved manifest.

Partitions = (business object type, owning company code). Each partition is extracted by a worker, written to
the staging table, and checkpointed, so a restarted run skips completed partitions. Source-load throttling is
modelled as a records-per-second budget per worker.

`ManifestExtractor` holds the manifest-driven planning and the carve-out semantics (organisational views of
shared masters, cross-company documents split at the company-code boundary, reference stubs); the concrete
adapters only differ in where rows come from: the in-memory synthetic store (`SyntheticStoreExtractor`) or the
SAP add-on through RFC (`RfcExtractor`, predicate pushdown per package, see runtime/rfc.py).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable, Protocol

from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.store import RecordStore
from ..catalog.tables import TABLES, record_key
from ..graph.service import split_node
from ..staging import StagedRow, get_backend
from . import rfc_config
from .adapters import ExtractedRecord, Partition
from .rfc import AbapAddonClient, RfcTransport, make_transport, predicate

TRANSFER_CLASSES = ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")


@dataclass
class ExtractionPlan:
    partitions: list[Partition]
    objects_by_partition: dict[str, list[str]]
    snapshot_id: str


class RowSource(Protocol):
    """What the carve-out semantics need from a source: keyed access and lookups by one field."""

    def by_key(self, table: str, key: str) -> dict | None: ...
    def lookup(self, table: str, field: str, value) -> list[dict]: ...
    def get(self, table: str, **key_fields) -> dict | None: ...


class ManifestExtractor:
    name = "MANIFEST"
    status = "PLANNED"

    def __init__(self, classification: dict[str, dict], scope_ccs: set[str], max_rows_per_second: int | None = None):
        self.classification = classification
        self.scope_ccs = scope_ccs
        self.max_rps = max_rows_per_second
        self._plan: ExtractionPlan | None = None
        self._lock = threading.Lock()
        self.retained_rows = 0
        self.retained_headers: set[str] = set()
        self.reference_stubs: set[str] = set()

    def snapshot(self) -> str:  # pragma: no cover - adapters override
        raise NotImplementedError

    def _source_for(self, partition: Partition) -> RowSource:  # pragma: no cover - adapters override
        raise NotImplementedError

    def adapter_stats(self) -> dict:
        return {}

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

    def _rows_for_object(self, rows: RowSource, bo_id: str, key: str, stub: bool = False) -> Iterable[tuple[str, dict]]:
        bo = BUSINESS_OBJECTS[bo_id]
        header = rows.by_key(bo.header_table, key)
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
            for row in rows.lookup(it, first, hk[first]):
                if not all(str(row.get(k)) == str(hk[k]) for k in common):
                    continue
                # company-code / plant views of client-level master data: only views of carved-out org units move
                if it in ("KNB1", "LFB1") and row.get("BUKRS") not in self.scope_ccs:
                    continue
                if it in ("MARC", "MBEW", "MARD", "MAST", "MAPL", "MCHA", "MCHB"):
                    k = rows.get("T001K", BWKEY=row.get("WERKS") or row.get("BWKEY"))
                    if not k or k["BUKRS"] not in self.scope_ccs:
                        continue
                yield it, row

    def extract(self, partition: Partition) -> Iterable[ExtractedRecord]:
        plan = self.plan()
        bo_id = partition.object_type
        rows = self._source_for(partition)
        started = time.monotonic()
        n = 0
        for nid in plan.objects_by_partition.get(partition.id, []):
            _, key = split_node(nid)
            c = self.classification[nid]
            stub = c["classification"] == "REFERENCE_ONLY"
            partial = c["classification"] == "PARTIALLY_TRANSFERRED"
            bo = BUSINESS_OBJECTS[bo_id]
            for table, row in self._rows_for_object(rows, bo_id, key, stub=stub):
                td = TABLES.get(table)
                # partially transferred cross-company documents: only the carved-out company code's side moves;
                # rows owned by retained company codes stay in the source (counted by the caller via node tags)
                if partial and td and td.org_field and td.org_field.startswith("BUKRS") and row.get(td.org_field) and row.get(td.org_field) not in self.scope_ccs:
                    with self._lock:
                        self.retained_rows += 1
                        if table == bo.header_table:
                            self.retained_headers.add(nid)
                    if table == bo.header_table:
                        break
                    continue
                if partial and td and td.org_field in ("WERKS", "DWERK"):
                    k = rows.get("T001K", BWKEY=row.get(td.org_field))
                    if k and k["BUKRS"] not in self.scope_ccs:
                        with self._lock:
                            self.retained_rows += 1
                            if table == bo.header_table:
                                self.retained_headers.add(nid)
                        if table == bo.header_table:
                            break
                        continue
                if stub:
                    with self._lock:
                        self.reference_stubs.add(nid)
                n += 1
                if self.max_rps:
                    elapsed = time.monotonic() - started
                    if n / max(elapsed, 1e-6) > self.max_rps:
                        time.sleep(n / self.max_rps - elapsed)
                yield ExtractedRecord(table=table, key=record_key(table, row), payload=row, object_node=nid)


class SyntheticStoreExtractor(ManifestExtractor):
    name = "SYNTHETIC"
    status = "SIMULATED"

    def __init__(self, store: RecordStore, classification: dict[str, dict], scope_ccs: set[str], max_rows_per_second: int | None = None):
        super().__init__(classification, scope_ccs, max_rows_per_second)
        self.store = store

    def snapshot(self) -> str:
        """Consistency token: hash of per-table row counts. A production adapter obtains a database snapshot /
        consistent read token from the source instead (the RFC adapter does, through Z_SDTF_OPEN_SNAPSHOT)."""
        h = hashlib.sha256()
        for t in self.store.tables():
            h.update(f"{t}:{self.store.count(t)};".encode())
        return "snap-" + h.hexdigest()[:16]

    def _source_for(self, partition: Partition) -> RowSource:
        return self.store


class _PartitionCache:
    """Rows of one partition fetched through RFC, indexed the way the carve-out semantics read them."""

    def __init__(self, shared: dict[str, dict[str, dict]]):
        self._tables: dict[str, dict[str, dict]] = dict(shared)
        self._idx: dict[tuple[str, str], dict[str, list[dict]]] = {}

    def add(self, table: str, rows: dict[str, dict]) -> None:
        self._tables.setdefault(table, {}).update(rows)

    def by_key(self, table: str, key: str) -> dict | None:
        return self._tables.get(table, {}).get(key)

    def get(self, table: str, **key_fields) -> dict | None:
        td = TABLES[table]
        return self.by_key(table, "|".join(str(key_fields.get(k, "")) for k in td.key_fields))

    def lookup(self, table: str, field: str, value) -> list[dict]:
        ix = self._idx.get((table, field))
        if ix is None:
            ix = defaultdict(list)
            for r in self._tables.get(table, {}).values():
                ix[str(r.get(field))].append(r)
            self._idx[(table, field)] = ix
        return ix.get(str(value), [])


class RfcExtractor(ManifestExtractor):
    """Extraction through the SAP add-on (sap-abap/README.md) over an RFC transport.

    Per partition it reads the business object's header table with the organisational predicate and, for small
    partitions, the object keys pushed down as EQ ranges; item tables are read by header-key ranges. Every
    package carries the run's consistency token and a checksum. The carve-out semantics are then applied exactly
    as for the synthetic adapter. T001K (valuation area -> company code) is read once and shared."""

    name = "RFC"
    status = "IMPLEMENTED"

    def __init__(self, transport: RfcTransport, classification: dict[str, dict], scope_ccs: set[str], max_rows_per_second: int | None = None, package_size: int | None = None, key_chunk: int | None = None, key_pushdown_limit: int | None = None):
        super().__init__(classification, scope_ccs, max_rows_per_second)
        self.client = AbapAddonClient(transport, package_size)
        self.key_chunk = key_chunk or rfc_config.key_chunk()
        self.key_pushdown_limit = key_pushdown_limit or rfc_config.key_pushdown_limit()
        self._shared: dict[str, dict[str, dict]] | None = None
        self.rows_transferred = 0
        self.rows_discarded = 0
        self.pushdown = {"org_predicate": 0, "key_ranges": 0, "org_only": 0}

    def snapshot(self) -> str:
        if not self.client.snapshot:
            tables = sorted({BUSINESS_OBJECTS[c["type"]].header_table for c in self.classification.values() if c["type"] in BUSINESS_OBJECTS} | {"T001K"})
            self.client.open_snapshot(tables)
        return self.client.snapshot  # type: ignore[return-value]

    def _shared_tables(self) -> dict[str, dict[str, dict]]:
        with self._lock:
            if self._shared is None:
                self.snapshot()
                self._shared = {"T001K": self.client.read_keyed("T001K", [])}
            return self._shared

    def _read_by_ranges(self, table: str, field: str, values: list[str], base: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        vals = sorted(set(values))
        for i in range(0, len(vals), self.key_chunk):
            chunk = vals[i : i + self.key_chunk]
            self.pushdown["key_ranges"] += 1
            out.update(self.client.read_keyed(table, base + [predicate(field, "EQ", v) for v in chunk]))
        return out

    def _source_for(self, partition: Partition) -> RowSource:
        plan = self.plan()
        bo = BUSINESS_OBJECTS[partition.object_type]
        htd = TABLES[bo.header_table]
        keys = [split_node(n)[1] for n in plan.objects_by_partition.get(partition.id, [])]
        wanted = set(keys)
        cache = _PartitionCache(self._shared_tables())
        owner = partition.predicate.get("company_code", "*")
        base: list[dict] = []
        if owner != "*" and htd.org_field and htd.org_field.startswith("BUKRS"):
            base.append(predicate(htd.org_field, "EQ", owner))
            self.pushdown["org_predicate"] += 1
        for f, v in (bo.header_filter or {}).items():  # shared header table: only this type's headers are read
            base.append(predicate(f, "EQ", v))
            self.pushdown["header_filter"] = self.pushdown.get("header_filter", 0) + 1
        # push the document/master number down; tables keyed only by an organisational field (T001, ...) push that key
        push_field = next((k for k in htd.key_fields if k not in (htd.org_field, htd.year_field)), htd.key_fields[0] if htd.key_fields else None)
        if push_field and len(keys) <= self.key_pushdown_limit:
            idx = htd.key_fields.index(push_field)
            headers = self._read_by_ranges(bo.header_table, push_field, [k.split("|")[idx] for k in keys], base)
        else:
            self.pushdown["org_only"] += 1
            headers = self.client.read_keyed(bo.header_table, base)
        self.rows_transferred += len(headers)
        self.rows_discarded += sum(1 for k in headers if k not in wanted)
        headers = {k: r for k, r in headers.items() if k in wanted}
        cache.add(bo.header_table, headers)
        hk = {k: {f: r[f] for f in bo.key_fields} for k, r in headers.items()}
        for it in bo.item_tables:
            td = TABLES.get(it)
            if td is None:
                continue
            common = [k for k in bo.key_fields if k in td.fields]
            if not common or not hk:
                continue
            first = common[0]
            items = self._read_by_ranges(it, first, [str(v[first]) for v in hk.values()], [])
            self.rows_transferred += len(items)
            cache.add(it, items)
        return cache

    def adapter_stats(self) -> dict:
        return {"adapter": self.name, "transport": getattr(self.client.t, "name", "?"), "rfc_calls": self.client.calls, "packages": self.client.packages, "rows_transferred": self.client.rows, "package_size": self.client.package_size, "pushdown": dict(self.pushdown), "snapshot_valid_until": self.client.valid_until, "cdc_watermark": self.client.cdc_watermark}


def build_extractor(session: Session, source, classification: dict[str, dict], scope_ccs: set[str], store: RecordStore | None = None) -> ManifestExtractor:
    """Adapter for a source system by its connector. SYNTHETIC reads the in-memory record store; RFC talks to the
    add-on through the transport configured for the system (simulated add-on or pyrfc)."""
    if source.connector == "SYNTHETIC":
        return SyntheticStoreExtractor(store or RecordStore.load(session, source.id), classification, scope_ccs)
    if source.connector == "RFC":
        transport = make_transport(source.sid, source.meta, store_loader=lambda: store or RecordStore.load(session, source.id))
        return RfcExtractor(transport, classification, scope_ccs)
    raise NotImplementedError(f"{source.connector} adapter is planned; see docs/02-sap-connectivity-design.md")


def extract_partition(extractor: ManifestExtractor, partition: Partition, run_id: str, backend) -> int:
    """Extract one partition into the staging backend. Idempotent: duplicate (table, key) rows are ignored."""
    seen = set()
    rows = []
    for r in extractor.extract(partition):
        if (r.table, r.key) in seen:
            continue
        seen.add((r.table, r.key))
        rows.append(StagedRow(partition.id, r.table, r.key, r.payload))
    return backend.write_partition(run_id, partition.id, rows)


def run_extraction(session: Session, run_id: str, extractor: ManifestExtractor, checkpoint: dict, workers: int = 4, backend=None) -> dict:
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
    metrics["adapter"] = extractor.name
    if extractor.adapter_stats():
        metrics["adapter_stats"] = extractor.adapter_stats()
    return metrics

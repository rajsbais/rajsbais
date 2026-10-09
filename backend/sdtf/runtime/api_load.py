"""Initial load through the released APIs of the target (ADR-0015, second step).

The LOAD stage groups the run's transformed staging records by business object instance and drives the same
loaders the delta cycles use: deep inserts and item creates for documents, business partner / product / cost
object creates for masters, journal entry postings with target-assigned numbers for accounting documents, the
migration cockpit for cockpit objects, histories and tables the document APIs do not expose, and configuration
matching for the target shell. Keys and images the target assigns are written back to staging, so reconciliation
compares against what the target holds. Re-runs and concurrent partitions are safe: existing documents are
compared (duplicate or conflict), journal entries are looked up by their source reference before posting.
"""
from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES
from ..models import MigrationRun, SapSystem, TransformationException
from ..staging import get_backend
from .loaders import DeltaLoader, EventView, object_of
from .target_api import TargetApiClient, make_target_transport

STATUS_MAP = {"APPLIED": "LOADED", "SKIPPED_DUPLICATE": "LOADED", "MATCHED": "MATCHED", "CONFIG_MISSING": "CONFIG_MISSING", "UNSUPPORTED": "UNSUPPORTED", "REJECTED_BY_TARGET": "REJECTED", "CONFLICT": "CONFLICT", "SKIPPED_MISSING": "REJECTED"}


class ApiTargetLoader:
    """Same interface as SimulatedTargetLoader: `load(batch, partition) -> metrics`."""

    name = "API"
    status = "SIMULATED"  # the transport decides: simulated gateway or HTTPS

    def __init__(self, session: Session, target: SapSystem, run_id: str, on_conflict: str = "ERROR", backend=None):
        self.session = session
        self.target = target
        self.run_id = run_id
        self.on_conflict = on_conflict
        self.backend = backend or get_backend(session=session)
        self.transport = make_target_transport(session, target)
        self.client = TargetApiClient(self.transport)
        run = session.get(MigrationRun, run_id)
        src = session.get(SapSystem, run.source_system_id) if run else None
        self.loader = DeltaLoader(self.client, target.product, read_row=getattr(self.transport, "row", None), source_ref_prefix=(src.logical_system or src.id) if src else "")

    def load(self, batch: int = 500, partition: str | None = None) -> dict:
        t0 = time.monotonic()
        m = {"loaded": 0, "matched_config": 0, "config_missing": 0, "updated_idempotent": 0, "skipped_duplicate": 0, "conflicts": 0, "unsupported": 0, "rejected": 0, "assigned_keys": 0, "by_table": defaultdict(int), "by_method": defaultdict(int), "by_api_call": defaultdict(int)}
        recs = list(self.backend.iter_records(self.run_id, status="TRANSFORMED", partition=partition))
        groups: dict[tuple[str, str], list] = defaultdict(list)
        order: list[tuple[str, str]] = []
        for rec in recs:
            bo_id, okey = object_of(rec.table_name, rec.target_key or rec.record_key, rec.target_payload)
            g = (bo_id, okey)
            if g not in groups:
                order.append(g)
            groups[g].append(rec)
        exceptions = []
        for g in order:
            members = groups[g]
            bo = BUSINESS_OBJECTS.get(g[0])
            # headers first, then items, in the catalog's order
            rank = {bo.header_table: 0, **{t: i + 1 for i, t in enumerate(bo.item_tables)}} if bo else {}
            members.sort(key=lambda r: (rank.get(r.table_name, 99), r.record_key))
            views = [EventView(0, r.table_name, "I", r.record_key, r.target_key, dict(r.target_payload or {}), g[0], g[1], g[1]) for r in members]
            self.loader.load_change_set(views, initial=True)
            for rec, v in zip(members, views, strict=True):
                res = v.result
                if res is None:
                    rec.load_status = "UNSUPPORTED"
                    m["unsupported"] += 1
                    continue
                rec.load_status = STATUS_MAP.get(res.status, "REJECTED")
                m["by_method"][res.load_method or "-"] += 1
                if res.api_call:
                    m["by_api_call"][res.api_call] += 1
                if res.status == "APPLIED":
                    m["loaded"] += 1
                    m["by_table"][rec.table_name] += 1
                    if res.target_key and res.target_key != rec.target_key:
                        m["assigned_keys"] += 1
                        rec.target_key = res.target_key
                    if res.target_row is not None:
                        rec.target_payload = res.target_row
                    rec.lineage = (rec.lineage or []) + [{"rule": "load", "field": "*", "from": res.load_method, "to": res.api_call}]
                elif res.status == "SKIPPED_DUPLICATE":
                    m["skipped_duplicate"] += 1
                    if res.target_key and res.target_key != rec.target_key:
                        rec.target_key = res.target_key
                    if res.target_row is not None:
                        rec.target_payload = res.target_row
                elif res.status == "MATCHED":
                    m["matched_config"] += 1
                elif res.status == "CONFIG_MISSING":
                    m["config_missing"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=rec.table_name, record_key=rec.target_key or "", rule_id="", severity="ERROR", message=f"organisational configuration {rec.table_name} {rec.target_key} does not exist in the target shell"))
                elif res.status == "CONFLICT":
                    m["conflicts"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=rec.table_name, record_key=rec.target_key or "", rule_id="", severity="ERROR", message=res.message or "target key already exists with different content (duplicate detection)"))
                elif res.status == "UNSUPPORTED":
                    m["unsupported"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=rec.table_name, record_key=rec.record_key, rule_id="", severity="WARN", message=res.message or f"no supported load method for {rec.table_name} into {self.target.product}"))
                else:
                    m["rejected"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=rec.table_name, record_key=rec.record_key, rule_id="", severity="ERROR", message=f"{res.api_call}: {res.message}"))
        self.backend.update_records(self.run_id, recs)
        self.session.add_all(exceptions)
        self.session.flush()
        m["by_table"] = dict(m["by_table"])
        m["by_method"] = dict(m["by_method"])
        m["by_api_call"] = dict(sorted(m["by_api_call"].items()))
        m["api"] = {"transport": getattr(self.transport, "name", "?"), **self.client.stats()}
        m["duration_s"] = round(time.monotonic() - t0, 3)
        return m


def build_loader(session: Session, target: SapSystem, run_id: str, backend=None, load_mode: str | None = None, on_conflict: str = "ERROR"):
    """`api` (default): released-API loaders over the target transport; `direct`: the simulated direct loader
    (kept for comparison runs and for targets that are nothing but a record store)."""
    from .load import SimulatedTargetLoader

    mode = (load_mode or __import__("os").getenv("SDTF_LOAD_MODE", "api")).lower()
    if mode == "direct":
        return SimulatedTargetLoader(session, target, run_id, on_conflict=on_conflict, backend=backend)
    return ApiTargetLoader(session, target, run_id, on_conflict=on_conflict, backend=backend)


__all__ = ["ApiTargetLoader", "build_loader", "object_of", "TABLES"]

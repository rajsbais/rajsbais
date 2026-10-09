"""Simulated target load with idempotent upsert and duplicate/conflict detection.

The simulated loader writes into the target system's record store and tags each table with the load method
that the S/4HANA compatibility registry prescribes (API, migration cockpit, config transport). It performs
no direct writes to a real SAP system. Production loaders implement the same interface per business object
package and target release.
"""
from __future__ import annotations

import time
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, load_methods_for
from ..catalog.tables import TABLES
from ..models import SapRecord, SapSystem, StagedRecord, TransformationException

_TABLE_TO_BO = {}
for _bo in BUSINESS_OBJECTS.values():
    _TABLE_TO_BO.setdefault(_bo.header_table, _bo.id)
    for _t in _bo.item_tables:
        _TABLE_TO_BO.setdefault(_t, _bo.id)


def load_method_for_table(table: str, target_product: str) -> str:
    bo_id = _TABLE_TO_BO.get(table)
    if not bo_id:
        return "UNMAPPED"
    methods = load_methods_for(BUSINESS_OBJECTS[bo_id], target_product)
    if not methods:
        return "UNSUPPORTED_FOR_TARGET"
    return methods[0].method


class SimulatedTargetLoader:
    name = "SIMULATED"
    status = "SIMULATED"

    def __init__(self, session: Session, target: SapSystem, run_id: str, on_conflict: str = "ERROR"):
        self.session = session
        self.target = target
        self.run_id = run_id
        self.on_conflict = on_conflict

    def load(self, batch: int = 500) -> dict:
        t0 = time.monotonic()
        m = {"loaded": 0, "matched_config": 0, "config_missing": 0, "updated_idempotent": 0, "skipped_duplicate": 0, "conflicts": 0, "unsupported": 0, "by_table": defaultdict(int), "by_method": defaultdict(int)}
        existing = {}
        for r in self.session.execute(select(SapRecord).where(SapRecord.system_id == self.target.id)).scalars():
            existing[(r.table_name, r.record_key)] = r
        stmt = select(StagedRecord).where(StagedRecord.run_id == self.run_id, StagedRecord.load_status == "TRANSFORMED").execution_options(yield_per=batch)
        new_rows = []
        exceptions = []
        for rec in self.session.execute(stmt).scalars():
            table = rec.table_name
            method = load_method_for_table(table, self.target.product)
            m["by_method"][method] += 1
            if method in ("DIRECT_TABLE_UNSUPPORTED", "UNSUPPORTED_FOR_TARGET"):
                rec.load_status = "UNSUPPORTED"
                m["unsupported"] += 1
                exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=table, record_key=rec.record_key, rule_id="", severity="WARN", message=f"no supported load method for {table} into {self.target.product}"))
                continue
            payload = dict(rec.target_payload)
            key = rec.target_key
            td = TABLES.get(table)
            prev = existing.get((table, key))
            if method == "CONFIG_TRANSPORT":
                # organisational configuration comes from the prepared target shell: records are matched, never loaded
                if prev is not None:
                    rec.load_status = "MATCHED"
                    m["matched_config"] += 1
                else:
                    rec.load_status = "CONFIG_MISSING"
                    m["config_missing"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=table, record_key=key, rule_id="", severity="ERROR", message=f"organisational configuration {table} {key} does not exist in the target shell"))
                continue
            if prev is not None:
                if prev.payload == payload:
                    rec.load_status = "LOADED"
                    m["skipped_duplicate"] += 1
                    continue
                if self.on_conflict == "OVERWRITE":
                    prev.payload = payload
                    rec.load_status = "LOADED"
                    m["updated_idempotent"] += 1
                    continue
                rec.load_status = "CONFLICT"
                m["conflicts"] += 1
                exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=table, record_key=key, rule_id="", severity="ERROR", message="target key already exists with different content (duplicate detection)"))
                continue
            org = td.org_field if td else None
            row = {
                "system_id": self.target.id,
                "table_name": table,
                "record_key": key,
                "bukrs": payload.get("BUKRS") or (payload.get(org) if org and org.startswith("BUKRS") else None),
                "werks": payload.get("WERKS") or (payload.get(org) if org in ("WERKS", "DWERK") else None),
                "gjahr": int(payload[td.year_field]) if td and td.year_field and payload.get(td.year_field) else None,
                "payload": payload,
            }
            new_rows.append(row)
            existing[(table, key)] = SapRecord(**row)
            rec.load_status = "LOADED"
            m["loaded"] += 1
            m["by_table"][table] += 1
            if len(new_rows) >= batch:
                self.session.execute(SapRecord.__table__.insert(), new_rows)
                new_rows = []
        if new_rows:
            self.session.execute(SapRecord.__table__.insert(), new_rows)
        self.session.add_all(exceptions)
        self.session.flush()
        m["by_table"] = dict(m["by_table"])
        m["by_method"] = dict(m["by_method"])
        m["duration_s"] = round(time.monotonic() - t0, 3)
        return m

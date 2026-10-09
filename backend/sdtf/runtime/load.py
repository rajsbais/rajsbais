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
from ..models import SapRecord, SapSystem, TransformationException
from ..staging import get_backend

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

    def __init__(self, session: Session, target: SapSystem, run_id: str, on_conflict: str = "ERROR", backend=None):
        self.session = session
        self.target = target
        self.run_id = run_id
        self.on_conflict = on_conflict
        self.backend = backend or get_backend(session=session)

    def load(self, batch: int = 500, partition: str | None = None) -> dict:
        """Load TRANSFORMED records (optionally of one partition). Safe under concurrent partition jobs: existence is
        checked for the partition's keys only, inserts use conflict-ignore semantics, and a record that lost an insert
        race is re-read and classified as duplicate or conflict."""
        t0 = time.monotonic()
        m = {"loaded": 0, "matched_config": 0, "config_missing": 0, "updated_idempotent": 0, "skipped_duplicate": 0, "conflicts": 0, "unsupported": 0, "by_table": defaultdict(int), "by_method": defaultdict(int)}
        recs = list(self.backend.iter_records(self.run_id, status="TRANSFORMED", partition=partition))
        existing = self._existing({(r.table_name, r.target_key) for r in recs})
        exceptions = []
        pending: list[tuple] = []
        for rec in recs:
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
            prev = existing.get((table, key))
            if method == "CONFIG_TRANSPORT":
                if prev is not None:
                    rec.load_status = "MATCHED"
                    m["matched_config"] += 1
                else:
                    rec.load_status = "CONFIG_MISSING"
                    m["config_missing"] += 1
                    exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=table, record_key=key, rule_id="", severity="ERROR", message=f"organisational configuration {table} {key} does not exist in the target shell"))
                continue
            if prev is not None:
                self._classify_existing(rec, prev, payload, m, exceptions)
                continue
            pending.append((rec, self._row(table, key, payload)))
        # insert new rows with conflict-ignore; detect lost races afterwards
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            self._insert_ignore([row for _, row in chunk])
            won = self._existing({(row["table_name"], row["record_key"]) for _, row in chunk})
            for rec, row in chunk:
                got = won.get((row["table_name"], row["record_key"]))
                if got is not None and got == row["payload"]:
                    rec.load_status = "LOADED"
                    m["loaded"] += 1
                    m["by_table"][row["table_name"]] += 1
                elif got is not None:
                    self._classify_existing(rec, got, row["payload"], m, exceptions)
                else:  # pragma: no cover - insert neither succeeded nor conflicted
                    rec.load_status = "CONFLICT"
                    m["conflicts"] += 1
        self.backend.update_records(self.run_id, recs)
        self.session.add_all(exceptions)
        self.session.flush()
        m["by_table"] = dict(m["by_table"])
        m["by_method"] = dict(m["by_method"])
        m["duration_s"] = round(time.monotonic() - t0, 3)
        return m

    def _classify_existing(self, rec, prev_payload: dict, payload: dict, m: dict, exceptions: list) -> None:
        if prev_payload == payload:
            rec.load_status = "LOADED"
            m["skipped_duplicate"] += 1
        elif self.on_conflict == "OVERWRITE":
            self.session.execute(SapRecord.__table__.update().where(SapRecord.system_id == self.target.id, SapRecord.table_name == rec.table_name, SapRecord.record_key == rec.target_key).values(payload=payload))
            rec.load_status = "LOADED"
            m["updated_idempotent"] += 1
        else:
            rec.load_status = "CONFLICT"
            m["conflicts"] += 1
            exceptions.append(TransformationException(run_id=self.run_id, stage="LOAD", table_name=rec.table_name, record_key=rec.target_key, rule_id="", severity="ERROR", message="target key already exists with different content (duplicate detection)"))

    def _row(self, table: str, key: str, payload: dict) -> dict:
        td = TABLES.get(table)
        org = td.org_field if td else None
        return {
            "system_id": self.target.id,
            "table_name": table,
            "record_key": key,
            "bukrs": payload.get("BUKRS") or (payload.get(org) if org and org.startswith("BUKRS") else None),
            "werks": payload.get("WERKS") or (payload.get(org) if org in ("WERKS", "DWERK") else None),
            "gjahr": int(payload[td.year_field]) if td and td.year_field and payload.get(td.year_field) else None,
            "payload": payload,
        }

    def _existing(self, keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        out: dict[tuple[str, str], dict] = {}
        by_table: dict[str, list[str]] = defaultdict(list)
        for t, k in keys:
            if k is not None:
                by_table[t].append(k)
        for t, ks in by_table.items():
            for i in range(0, len(ks), 500):
                for r in self.session.execute(select(SapRecord.record_key, SapRecord.payload).where(SapRecord.system_id == self.target.id, SapRecord.table_name == t, SapRecord.record_key.in_(ks[i : i + 500]))):
                    out[(t, r[0])] = r[1]
        return out

    def _insert_ignore(self, rows: list[dict]) -> None:
        if not rows:
            return
        dialect = self.session.get_bind().dialect.name
        if dialect == "sqlite":
            stmt = SapRecord.__table__.insert().prefix_with("OR IGNORE")
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(SapRecord.__table__).on_conflict_do_nothing(constraint="uq_sap_record")
        else:  # pragma: no cover
            stmt = SapRecord.__table__.insert()
        self.session.execute(stmt, rows)
        self.session.flush()

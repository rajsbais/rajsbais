"""In-memory indexed view over a system's SAP-like records.

The vertical slice loads one system's records into memory with per-table indexes. For multi-terabyte
landscapes the same interface is backed by partitioned object storage and pushdown queries (planned);
callers only use `rows(table)`, `by_key`, and `index(table, field)`.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.tables import TABLES, record_key
from ..models import SapRecord


class RecordStore:
    def __init__(self, system_id: str):
        self.system_id = system_id
        self._tables: dict[str, list[dict]] = defaultdict(list)
        self._by_key: dict[str, dict[str, dict]] = defaultdict(dict)
        self._indexes: dict[tuple[str, str], dict[str, list[dict]]] = {}

    @classmethod
    def load(cls, session: Session, system_id: str, tables: Iterable[str] | None = None) -> RecordStore:
        store = cls(system_id)
        stmt = select(SapRecord.table_name, SapRecord.record_key, SapRecord.payload).where(SapRecord.system_id == system_id)
        if tables:
            stmt = stmt.where(SapRecord.table_name.in_(list(tables)))
        for table_name, key, payload in session.execute(stmt):
            store._tables[table_name].append(payload)
            store._by_key[table_name][key] = payload
        return store

    @classmethod
    def from_tables(cls, system_id: str, tables: dict[str, list[dict]]) -> RecordStore:
        store = cls(system_id)
        for t, rows in tables.items():
            for r in rows:
                store._tables[t].append(r)
                store._by_key[t][record_key(t, r)] = r
        return store

    def tables(self) -> list[str]:
        return sorted(t for t, rows in self._tables.items() if rows)

    def rows(self, table: str) -> list[dict]:
        return self._tables.get(table, [])

    def count(self, table: str) -> int:
        return len(self._tables.get(table, []))

    def by_key(self, table: str, key: str) -> dict | None:
        return self._by_key.get(table, {}).get(key)

    def get(self, table: str, **key_fields) -> dict | None:
        td = TABLES[table]
        key = "|".join(str(key_fields.get(k, "")) for k in td.key_fields)
        return self.by_key(table, key)

    def index(self, table: str, field: str) -> dict[str, list[dict]]:
        k = (table, field)
        if k not in self._indexes:
            idx: dict[str, list[dict]] = defaultdict(list)
            for r in self._tables.get(table, []):
                idx[str(r.get(field, ""))].append(r)
            self._indexes[k] = idx
        return self._indexes[k]

    def lookup(self, table: str, field: str, value) -> list[dict]:
        return self.index(table, field).get(str(value), [])


def import_tables(session: Session, system_id: str, tables: dict[str, list[dict]], batch: int = 2000) -> dict[str, int]:
    """Persist generated/extracted tables into the generic record store. Idempotent on (system, table, key)."""
    counts: dict[str, int] = {}
    existing = {
        (t, k)
        for t, k in session.execute(select(SapRecord.table_name, SapRecord.record_key).where(SapRecord.system_id == system_id))
    }
    buf: list[dict] = []
    for table, rows in tables.items():
        td = TABLES.get(table)
        if td is None:
            continue
        n = 0
        for r in rows:
            key = record_key(table, r)
            if (table, key) in existing:
                continue
            existing.add((table, key))
            n += 1
            org = td.org_field
            bukrs = r.get("BUKRS") or (r.get(org) if org and org.startswith("BUKRS") else None)
            werks = r.get("WERKS") or (r.get(org) if org and org in ("WERKS", "DWERK") else None)
            year_val = r.get(td.year_field) if td.year_field else None
            buf.append({"system_id": system_id, "table_name": table, "record_key": key, "bukrs": bukrs, "werks": werks, "gjahr": int(year_val) if year_val else None, "payload": r})
            if len(buf) >= batch:
                session.execute(SapRecord.__table__.insert(), buf)
                buf = []
        counts[table] = n
    if buf:
        session.execute(SapRecord.__table__.insert(), buf)
    session.flush()
    return counts


def _record_columns(table: str, row: dict) -> dict:
    td = TABLES[table]
    org = td.org_field
    bukrs = row.get("BUKRS") or (row.get(org) if org and org.startswith("BUKRS") else None)
    werks = row.get("WERKS") or (row.get(org) if org and org in ("WERKS", "DWERK") else None)
    year_val = row.get(td.year_field) if td.year_field else None
    return {"bukrs": bukrs, "werks": werks, "gjahr": int(year_val) if year_val else None, "payload": row}


def upsert_records(session: Session, system_id: str, table: str, rows: Iterable[dict]) -> dict[str, int]:
    """Insert or replace rows of one table in a system's record store (delta apply, simulated source changes)."""
    from sqlalchemy import update

    n = {"inserted": 0, "updated": 0}
    for r in rows:
        key = record_key(table, r)
        cols = _record_columns(table, r)
        res = session.execute(update(SapRecord).where(SapRecord.system_id == system_id, SapRecord.table_name == table, SapRecord.record_key == key).values(**cols))
        if res.rowcount:
            n["updated"] += 1
        else:
            session.execute(SapRecord.__table__.insert(), [{"system_id": system_id, "table_name": table, "record_key": key, **cols}])
            n["inserted"] += 1
    session.flush()
    return n


def delete_records(session: Session, system_id: str, table: str, keys: Iterable[str]) -> int:
    from sqlalchemy import delete

    keys = list(keys)
    if not keys:
        return 0
    res = session.execute(delete(SapRecord).where(SapRecord.system_id == system_id, SapRecord.table_name == table, SapRecord.record_key.in_(keys)))
    session.flush()
    return res.rowcount or 0

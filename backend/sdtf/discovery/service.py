"""Read-only landscape discovery: system facts, organisational hierarchy, table statistics, business
object inventory, interfaces/jobs and S/4HANA impact items. Operates on a RecordStore so that the same
code runs against the synthetic landscape today and against RFC/CDS extracts later."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, instance_company_codes, instance_status
from ..catalog.store import RecordStore
from ..catalog.tables import TABLES, is_custom
from ..models import BusinessObjectInstance, DiscoverySnapshot, OrgUnit, SapSystem, TableStatistic

ORG_UNIT_SOURCES = [
    ("COMPANY_CODE", "T001", "BUKRS", "BUTXT", None, None),
    ("PLANT", "T001W", "WERKS", "NAME1", "COMPANY_CODE", "T001K"),
    ("SALES_ORG", "TVKO", "VKORG", "VTEXT", "COMPANY_CODE", None),
    ("PURCH_ORG", "T024E", "EKORG", "EKOTX", "COMPANY_CODE", None),
    ("CONTROLLING_AREA", "TKA01", "KOKRS", "BEZEI", None, None),
]


def discover_system(session: Session, system: SapSystem, actor: str, store: RecordStore | None = None) -> DiscoverySnapshot:
    store = store or RecordStore.load(session, system.id)
    snap = DiscoverySnapshot(system_id=system.id, created_by=actor, status="RUNNING")
    session.add(snap)
    session.flush()

    # ---- org units (replace previous discovery)
    session.execute(delete(OrgUnit).where(OrgUnit.system_id == system.id))
    org_counts: Counter = Counter()
    for unit_type, table, code_f, name_f, parent_type, parent_table in ORG_UNIT_SOURCES:
        for r in store.rows(table):
            parent_code = None
            if parent_type == "COMPANY_CODE":
                if parent_table == "T001K":
                    k = store.get("T001K", BWKEY=r[code_f])
                    parent_code = k["BUKRS"] if k else None
                else:
                    parent_code = r.get("BUKRS")
            attrs = {k: v for k, v in r.items() if k not in (code_f, name_f)}
            if unit_type == "COMPANY_CODE":
                a = store.get("TKA02", BUKRS=r["BUKRS"])
                attrs["KOKRS"] = a["KOKRS"] if a else None
            session.add(OrgUnit(system_id=system.id, unit_type=unit_type, code=r[code_f], name=r.get(name_f, ""), parent_type=parent_type, parent_code=parent_code, attributes=attrs))
            org_counts[unit_type] += 1
    # cost / profit centers as org-like units
    for r in store.rows("CSKS"):
        session.add(OrgUnit(system_id=system.id, unit_type="COST_CENTER", code=r["KOSTL"], name=r["KTEXT"], parent_type="COMPANY_CODE", parent_code=r["BUKRS"], attributes={"KOKRS": r["KOKRS"], "PRCTR": r["PRCTR"]}))
        org_counts["COST_CENTER"] += 1
    for r in store.rows("CEPC"):
        session.add(OrgUnit(system_id=system.id, unit_type="PROFIT_CENTER", code=r["PRCTR"], name=r["KTEXT"], parent_type="COMPANY_CODE", parent_code=r["BUKRS"], attributes={"KOKRS": r["KOKRS"]}))
        org_counts["PROFIT_CENTER"] += 1

    # ---- table statistics
    session.execute(delete(TableStatistic).where(TableStatistic.system_id == system.id))
    total_bytes = 0
    table_summary = []
    for table in store.tables():
        td = TABLES.get(table)
        rows = store.rows(table)
        by_cc: Counter = Counter()
        by_year: Counter = Counter()
        for r in rows:
            cc = r.get("BUKRS") or (r.get(td.org_field) if td and td.org_field and td.org_field.startswith("BUKRS") else None)
            if not cc and td and td.org_field in ("WERKS", "DWERK"):
                k = store.get("T001K", BWKEY=r.get(td.org_field))
                cc = k["BUKRS"] if k else None
            if cc:
                by_cc[cc] += 1
            if td and td.year_field and r.get(td.year_field):
                by_year[str(r[td.year_field])] += 1
        est = len(rows) * (td.avg_row_bytes if td else 256)
        total_bytes += est
        session.add(TableStatistic(system_id=system.id, snapshot_id=snap.id, table_name=table, is_custom=is_custom(table), row_count=len(rows), est_bytes=est, by_company_code=dict(by_cc), by_fiscal_year=dict(by_year), key_fields=list(td.key_fields) if td else [], fields=list(td.fields) if td else []))
        table_summary.append({"table": table, "rows": len(rows), "est_bytes": est, "custom": is_custom(table), "s4_status": td.s4_status if td else "UNKNOWN"})

    # ---- business object inventory
    session.execute(delete(BusinessObjectInstance).where(BusinessObjectInstance.system_id == system.id))
    inventory: dict[str, dict] = {}
    buf = []
    for bo in BUSINESS_OBJECTS.values():
        rows = store.rows(bo.header_table)
        if not rows:
            continue
        inv = {"type": bo.id, "name": bo.name, "domain": bo.domain, "kind": bo.kind, "count": 0, "by_company_code": Counter(), "by_year": Counter(), "open": 0, "shared": 0}
        for r in rows:
            key = bo.key_of(r)
            ccs = instance_company_codes(bo, r, store)
            status = instance_status(bo, r, store)
            year = r.get(bo.year_field) if bo.year_field else None
            if isinstance(year, str) and len(year) >= 4:
                year = int(year[:4])
            werks = r.get("WERKS") or r.get("DWERK")
            inv["count"] += 1
            for c in ccs[:1]:
                inv["by_company_code"][c] += 1
            if year:
                inv["by_year"][str(year)] += 1
            if status == "OPEN":
                inv["open"] += 1
            if len(ccs) > 1:
                inv["shared"] += 1
            buf.append({"system_id": system.id, "object_type": bo.id, "object_key": key, "bukrs": ccs[0] if ccs else None, "werks": werks, "gjahr": int(year) if year else None, "status": status, "company_codes": ccs, "attributes": {}})
        inv["by_company_code"] = dict(inv["by_company_code"])
        inv["by_year"] = dict(inv["by_year"])
        inventory[bo.id] = inv
    if buf:
        session.execute(BusinessObjectInstance.__table__.insert(), buf)

    # ---- interfaces, jobs, custom objects, simplification impacts
    interfaces = [{"type": "RFC", "name": r["RFCDEST"], "target": r["RFCHOST"], "purpose": r["RFCOPTIONS"]} for r in store.rows("RFCDES")]
    interfaces += [{"type": "IDOC", "name": r["PARNUM"], "target": r["RCVPOR"], "purpose": r["MESTYP"]} for r in store.rows("EDPP1")]
    jobs = [{"name": r["JOBNAME"], "user": r["SDLUNAME"], "periodic": r["PERIODIC"] == "X"} for r in store.rows("TBTCO")]
    custom_tables = [t for t in store.tables() if is_custom(t)]
    s4_impacts = [
        {"table": t, "status": TABLES[t].s4_status, "note": TABLES[t].s4_note, "rows": store.count(t)}
        for t in store.tables()
        if t in TABLES and TABLES[t].s4_status != "RETAINED"
    ]

    complexity = _complexity_score(inventory, interfaces, custom_tables, store)
    snap.summary = {
        "system": {"sid": system.sid, "client": system.client, "product": system.product, "release": system.release, "database": system.database, "os": system.os_name, "logical_system": system.logical_system, "connector": system.connector, "connector_status": system.connector_status},
        "org_units": dict(org_counts),
        "tables": {"count": len(table_summary), "custom": len(custom_tables), "total_rows": sum(t["rows"] for t in table_summary), "est_bytes": total_bytes, "top": sorted(table_summary, key=lambda t: -t["rows"])[:15]},
        "business_objects": inventory,
        "interfaces": interfaces,
        "jobs": jobs,
        "custom_tables": custom_tables,
        "s4_impacts": s4_impacts,
        "complexity": complexity,
        "estimates": _estimates(total_bytes, inventory),
        "read": {"path": "record_store", "rows_read": sum(len(store.rows(t)) for t in store.tables())},
    }
    snap.status = "COMPLETE"
    session.flush()
    return snap


def _complexity_score(inventory, interfaces, custom_tables, store) -> dict:
    shared = sum(i["shared"] for i in inventory.values())
    cross_company_docs = sum(1 for r in store.rows("BKPF") if r.get("BVORG"))
    open_docs = sum(i["open"] for i in inventory.values() if i["kind"] == "TRANSACTIONAL")
    return _complexity_from({"company_codes": store.count("T001"), "plants": store.count("T001W"), "shared_master_data": shared, "cross_company_documents": cross_company_docs, "open_documents": open_docs, "interfaces": len(interfaces), "custom_tables": len(custom_tables)})


def _complexity_from(factors: dict) -> dict:
    score = min(100, int(factors["company_codes"] * 3 + factors["plants"] * 1.5 + factors["shared_master_data"] * 0.2 + factors["cross_company_documents"] * 0.3 + factors["interfaces"] * 2 + factors["custom_tables"] * 3))
    return {"score": score, "band": "HIGH" if score > 70 else ("MEDIUM" if score > 35 else "LOW"), "factors": factors}


def _estimates(total_bytes: int, inventory) -> dict:
    """Indicative only. Throughput numbers are benchmark placeholders to be replaced by measured values."""
    records = sum(i["count"] for i in inventory.values())
    assumed_records_per_second = 2500  # placeholder, see docs/benchmarks.md
    return {"est_bytes": total_bytes, "est_business_objects": records, "assumed_throughput_rec_s": assumed_records_per_second, "est_full_extract_seconds": round(records / assumed_records_per_second, 1), "disclaimer": "Estimates are derived from synthetic row counts and placeholder throughput; they are not a performance guarantee."}


def latest_snapshot(session: Session, system_id: str) -> DiscoverySnapshot | None:
    return session.execute(select(DiscoverySnapshot).where(DiscoverySnapshot.system_id == system_id).order_by(DiscoverySnapshot.created_at.desc())).scalars().first()


def discovery_completeness(session: Session, system_id: str) -> tuple[bool, str]:
    """Whether the latest discovery holds the complete instance inventory the scope engine classifies from: a
    record-store discovery always does; a discovery through the add-on only when it was not sampled."""
    snap = latest_snapshot(session, system_id)
    if snap is None:
        return False, "no discovery snapshot: run discovery first"
    read = (snap.summary or {}).get("read") or {}
    if read.get("path") == "rfc" and not read.get("complete", False):
        partial = [b for b, i in (snap.summary.get("business_objects") or {}).items() if i.get("sampled", 0) < i.get("count", 0)]
        return False, f"the discovery through the add-on was sampled ({read.get('sample')} instances per object type; incomplete for {', '.join(partial[:6])}{'…' if len(partial) > 6 else ''}): run a full discovery before scoping"
    return True, ""

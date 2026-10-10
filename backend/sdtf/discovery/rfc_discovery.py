"""Landscape discovery through the read-only add-on, for a source the platform does not hold a copy of: the
organisational tables and the interface tables are read in full (they are small), the tables of the catalogue
and the custom tables are sized from the DDIC and DB_GET_TABLE_SIZE (`Z_SDTF_TABLE_METADATA`), their distribution
by company code and fiscal year is counted in the system (`Z_SDTF_AGGREGATE`), and the business object inventory
is counted the same way with a bounded sample of instances read for the status and shared-data figures. Nothing
is scanned on the platform side; what the technical user may not read is listed, not guessed."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, instance_company_codes, instance_status
from ..catalog.store import RecordStore
from ..catalog.tables import TABLES, is_custom, record_key
from ..models import BusinessObjectInstance, DiscoverySnapshot, OrgUnit, SapSystem, TableStatistic
from ..runtime.addon import client_for
from ..runtime.rfc import AbapAddonClient, RfcError, RfcUnavailable, predicate
from .service import ORG_UNIT_SOURCES, _complexity_from, _estimates

ORG_TABLES = ("T001", "T001K", "T001W", "TVKO", "T024E", "TKA01", "TKA02", "CSKS", "CEPC")
INTERFACE_TABLES = ("RFCDES", "EDPP1", "TBTCO")
MAX_FULL_READ = 50_000  # an organisational or interface table larger than this is sampled, not read
SKIP_SIZING = {"DD02L", "DD02T", "DD03L"}
# rows the sampled instances need for their status and company codes: (table, lookup field, header field)
SAMPLE_DEPENDENCIES: dict[str, list[tuple[str, str, str]]] = {
    "MD.Customer": [("KNB1", "KUNNR", "KUNNR")],
    "MD.Vendor": [("LFB1", "LIFNR", "LIFNR")],
    "MD.Material": [("MARC", "MATNR", "MATNR")],
    "SD.SalesOrder": [("VBAP", "VBELN", "VBELN")],
    "SD.Delivery": [("LIPS", "VBELN", "VBELN"), ("VBFA", "VBELV", "VBELN")],
    "SD.BillingDocument": [("VBRP", "VBELN", "VBELN")],
    "MM.PurchaseOrder": [("EKPO", "EBELN", "EBELN"), ("EKBE", "EBELN", "EBELN")],
    "FI.AccountingDocument": [("BSID", "BELNR", "BELNR"), ("BSIK", "BELNR", "BELNR"), ("BKPF", "BVORG", "BVORG")],
    "PP.ProductionOrder": [("AFPO", "AUFNR", "AUFNR")],
    "MD.BillOfMaterial": [("MAST", "STLNR", "STLNR")],
    "MD.Routing": [("MAPL", "PLNNR", "PLNNR")],
    "MD.Batch": [("MCHA", "CHARG", "CHARG")],
    "Z.ExportControl": [("MARC", "MATNR", "MATNR")],
    "Z.SupplierExt": [("LFB1", "LIFNR", "LIFNR")],
}


# dependencies answered by an aggregate instead of rows: (table, lookup field, header field, group fields); the
# store receives one thin row per group (the group fields and COUNT), enough for the company-code derivation
AGGREGATE_DEPENDENCIES: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "MM.MaterialDocument": [("MSEG", "MBLNR", "MBLNR", ["MBLNR", "MJAHR", "BUKRS"])],
}
LEAN_NOTES = {
    "FI.AccountingDocument": "status from the open-item tables BSID / BSIK (compatibility views over ACDOCA on S/4HANA); BSEG not read",
    "MM.MaterialDocument": "company codes from MSEG aggregated by document and company code; MSEG rows not read",
}


def _chunks(values: list[str], n: int):
    for i in range(0, len(values), n):
        yield values[i : i + n]


def discover_over_rfc(session: Session, system: SapSystem, actor: str, sample: int | None = None, max_custom_tables: int = 200, client: AbapAddonClient | None = None) -> DiscoverySnapshot:
    """Discover `system` through the add-on and persist the same snapshot the record-store discovery writes.
    `sample=None` reads every header row of every business object (the scope engine classifies from this index,
    so it has to be complete); `sample=N` reads the first N instances per object type for a quick look whose status
    and shared figures are extrapolated and labelled, and whose snapshot the scope engine refuses to work from."""
    snap = DiscoverySnapshot(system_id=system.id, created_by=actor, status="RUNNING")
    session.add(snap)
    session.flush()
    read: dict = {"path": "rfc", "sample": sample, "complete": sample is None, "unreadable": {}, "notes": []}
    if client is None:
        try:
            client, transport = client_for(session, system, None, package_size=sample)
        except (RfcError, RfcUnavailable) as e:
            snap.status = "FAILED"
            snap.summary = {"system": _system_facts(system), "read": {**read, "error": str(e)}}
            session.flush()
            return snap
    else:
        transport = getattr(client.t, "name", "?")
        if not client.snapshot:
            client.open_snapshot()
    read["transport"] = transport
    store = RecordStore(system.id)  # the organisational and interface rows, in memory for this discovery only

    def read_full(table: str) -> list[dict]:
        try:
            md = client.table_metadata(table)
            if md["rows"] > MAX_FULL_READ:
                rows, _c, _eof = client.read_package(table, [])
                read["notes"].append(f"{table}: {md['rows']} rows, only the first {len(rows)} read")
            else:
                rows = list(client.read_all(table, []))
        except RfcError as e:
            read["unreadable"][table] = f"{e.key}: {e.message}"
            return []
        _add_rows(store, table, rows)
        return rows

    for t in ORG_TABLES + INTERFACE_TABLES:
        read_full(t)

    # ---- org units (same rules as the record-store discovery, from the rows just read)
    session.execute(delete(OrgUnit).where(OrgUnit.system_id == system.id))
    org_counts: Counter = Counter()
    for unit_type, table, code_f, name_f, parent_type, parent_table in ORG_UNIT_SOURCES:
        for r in store.rows(table):
            parent_code = None
            if parent_type == "COMPANY_CODE":
                if parent_table == "T001K":
                    k = store.get("T001K", BWKEY=r.get(code_f))
                    parent_code = k["BUKRS"] if k else None
                else:
                    parent_code = r.get("BUKRS")
            attrs = {k: v for k, v in r.items() if k not in (code_f, name_f)}
            if unit_type == "COMPANY_CODE":
                a = store.get("TKA02", BUKRS=r.get("BUKRS"))
                attrs["KOKRS"] = a["KOKRS"] if a else None
            session.add(OrgUnit(system_id=system.id, unit_type=unit_type, code=str(r.get(code_f, "")), name=str(r.get(name_f, "") or ""), parent_type=parent_type, parent_code=parent_code, attributes=attrs))
            org_counts[unit_type] += 1
    for r in store.rows("CSKS"):
        session.add(OrgUnit(system_id=system.id, unit_type="COST_CENTER", code=r["KOSTL"], name=r.get("KTEXT", ""), parent_type="COMPANY_CODE", parent_code=r.get("BUKRS"), attributes={"KOKRS": r.get("KOKRS"), "PRCTR": r.get("PRCTR")}))
        org_counts["COST_CENTER"] += 1
    for r in store.rows("CEPC"):
        session.add(OrgUnit(system_id=system.id, unit_type="PROFIT_CENTER", code=r["PRCTR"], name=r.get("KTEXT", ""), parent_type="COMPANY_CODE", parent_code=r.get("BUKRS"), attributes={"KOKRS": r.get("KOKRS")}))
        org_counts["PROFIT_CENTER"] += 1
    plant_cc = {str(r["BWKEY"]): str(r["BUKRS"]) for r in store.rows("T001K")}

    # ---- the tables: catalogue plus the custom tables of the DDIC
    custom: dict[str, str] = {}
    try:
        for r in client.read_all("DD02L", [predicate("TABNAME", "CP", "Z*"), predicate("TABNAME", "CP", "Y*"), predicate("TABCLASS", "EQ", "TRANSP"), predicate("AS4LOCAL", "EQ", "A")]):
            custom[str(r["TABNAME"])] = ""
            if len(custom) >= max_custom_tables:
                read["notes"].append(f"custom tables capped at {max_custom_tables}")
                break
        try:
            for chunk in _chunks(sorted(custom), 50):
                for r in client.read_all("DD02T", [predicate("DDLANGUAGE", "EQ", "E")] + [predicate("TABNAME", "EQ", t) for t in chunk]):
                    custom[str(r["TABNAME"])] = str(r.get("DDTEXT", ""))
        except RfcError as e:
            read["unreadable"]["DD02T"] = f"{e.key}: {e.message}"
    except RfcError as e:
        read["unreadable"]["DD02L"] = f"{e.key}: {e.message}"
    custom_fields: dict[str, list[dict]] = {}
    if custom:
        try:
            for chunk in _chunks(sorted(custom), 50):
                for r in client.read_all("DD03L", [predicate("AS4LOCAL", "EQ", "A")] + [predicate("TABNAME", "EQ", t) for t in chunk]):
                    custom_fields.setdefault(str(r["TABNAME"]), []).append(r)
        except RfcError as e:
            read["unreadable"]["DD03L"] = f"{e.key}: {e.message}"

    session.execute(delete(TableStatistic).where(TableStatistic.system_id == system.id))
    total_bytes = 0
    table_summary = []
    sized: dict[str, dict] = {}
    for table in sorted((set(TABLES) - SKIP_SIZING) | set(custom)):
        td = TABLES.get(table)
        try:
            md = client.table_metadata(table)
        except RfcError as e:
            if e.key in ("TABLE_UNKNOWN",):
                continue  # not on this release
            read["unreadable"][table] = f"{e.key}: {e.message}"
            continue
        if not md.get("authorized", True):
            read["unreadable"][table] = "NOT_AUTHORIZED: S_TABU_NAM"
            continue
        n = int(md["rows"])
        if n == 0 and table not in custom:
            continue  # empty standard table: not part of this landscape
        by_cc: Counter = Counter()
        by_year: Counter = Counter()
        custom_field_names = {str(f["FIELDNAME"]) for f in custom_fields.get(table, [])}
        cc_field = "BUKRS" if (td and "BUKRS" in td.fields) or (not td and "BUKRS" in custom_field_names) else (td.org_field if td and td.org_field and td.org_field.startswith("BUKRS") else None)
        if n:
            try:
                if cc_field:
                    for a in client.aggregate(table, [], [cc_field], []):
                        if str(a.get(cc_field, "")):
                            by_cc[str(a[cc_field])] += int(a["COUNT"])
                elif td and td.org_field in ("WERKS", "DWERK"):
                    for a in client.aggregate(table, [], [td.org_field], []):
                        cc = plant_cc.get(str(a.get(td.org_field, "")))
                        if cc:
                            by_cc[cc] += int(a["COUNT"])
                if td and td.year_field:
                    for a in client.aggregate(table, [], [td.year_field], []):
                        if str(a.get(td.year_field, "")):
                            by_year[str(a[td.year_field])] += int(a["COUNT"])
            except RfcError as e:
                read["notes"].append(f"{table}: distribution not counted ({e.key})")
        est = int(md["size_mb"] * 1048576) if md.get("size_mb") else n * (td.avg_row_bytes if td else 256)
        total_bytes += est
        fields = md["fields"] or (list(td.fields) if td else [str(f["FIELDNAME"]) for f in sorted(custom_fields.get(table, []), key=lambda f: str(f.get("POSITION", "")))])
        keys = md["key_fields"] or (list(td.key_fields) if td else [str(f["FIELDNAME"]) for f in custom_fields.get(table, []) if f.get("KEYFLAG") == "X"])
        sized[table] = md
        session.add(TableStatistic(system_id=system.id, snapshot_id=snap.id, table_name=table, is_custom=is_custom(table), row_count=n, est_bytes=est, by_company_code=dict(by_cc), by_fiscal_year=dict(by_year), key_fields=keys, fields=fields))
        table_summary.append({"table": table, "rows": n, "est_bytes": est, "custom": is_custom(table), "s4_status": td.s4_status if td else "UNKNOWN", "description": td.description if td else custom.get(table, "")})

    # ---- business object inventory: counted in the system, status and shared-data figures from a sample
    session.execute(delete(BusinessObjectInstance).where(BusinessObjectInstance.system_id == system.id))
    inventory: dict[str, dict] = {}
    buf = []
    for bo in BUSINESS_OBJECTS.values():
        md = sized.get(bo.header_table)
        if not md or not md["rows"]:
            continue
        inv = {"type": bo.id, "name": bo.name, "domain": bo.domain, "kind": bo.kind, "count": int(md["rows"]), "by_company_code": {}, "by_year": {}, "open": 0, "shared": 0, "sampled": 0}
        td = TABLES.get(bo.header_table)
        try:
            if bo.org_field and bo.org_field.startswith("BUKRS"):
                inv["by_company_code"] = {str(a[bo.org_field]): int(a["COUNT"]) for a in client.aggregate(bo.header_table, [], [bo.org_field], []) if str(a.get(bo.org_field, ""))}
            elif bo.org_field in ("WERKS", "DWERK") or (td and td.org_field in ("WERKS", "DWERK")):
                f = bo.org_field if bo.org_field in ("WERKS", "DWERK") else td.org_field
                c: Counter = Counter()
                for a in client.aggregate(bo.header_table, [], [f], []):
                    cc = plant_cc.get(str(a.get(f, "")))
                    if cc:
                        c[cc] += int(a["COUNT"])
                inv["by_company_code"] = dict(c)
            if bo.year_field:
                inv["by_year"] = {str(a[bo.year_field])[:4]: int(a["COUNT"]) for a in client.aggregate(bo.header_table, [], [bo.year_field], []) if str(a.get(bo.year_field, ""))}
        except RfcError as e:
            read["notes"].append(f"{bo.id}: distribution not counted ({e.key})")
        try:
            if sample is None:
                rows = []
                for r in client.read_all(bo.header_table, []):
                    rows.append(r)
                    if len(rows) % 5000 == 0:
                        _prefetch(client, store, bo.id, rows[-5000:], read)
                _prefetch(client, store, bo.id, rows[-(len(rows) % 5000) :] if len(rows) % 5000 else [], read)
            else:
                rows, _c, _eof = client.read_package(bo.header_table, [])
                _prefetch(client, store, bo.id, rows, read)
        except RfcError as e:
            read["notes"].append(f"{bo.id}: instances not read ({e.key})")
            rows = []
        inv["sampled"] = len(rows)
        if rows and inv["sampled"] == inv["count"]:
            # the sample is the whole table: the distributions follow the same rules as the record-store discovery
            cc_c: Counter = Counter()
            y_c: Counter = Counter()
            for r in rows:
                ccs = instance_company_codes(bo, r, store)
                for c in ccs[:1]:
                    cc_c[c] += 1
                y = r.get(bo.year_field) if bo.year_field else None
                if y:
                    y_c[str(y)[:4]] += 1
            inv["by_company_code"], inv["by_year"], inv["distribution"] = dict(cc_c), dict(y_c), "counted"
        else:
            inv["distribution"] = "counted" if inv["by_company_code"] else "sampled"
        for r in rows:
            ccs = instance_company_codes(bo, r, store)
            status = instance_status(bo, r, store)
            year = r.get(bo.year_field) if bo.year_field else None
            if isinstance(year, str) and len(year) >= 4:
                year = int(year[:4])
            if status == "OPEN":
                inv["open"] += 1
            if len(ccs) > 1:
                inv["shared"] += 1
            buf.append({"system_id": system.id, "object_type": bo.id, "object_key": bo.key_of(r), "bukrs": ccs[0] if ccs else None, "werks": r.get("WERKS") or r.get("DWERK"), "gjahr": int(year) if year else None, "status": status, "company_codes": ccs, "attributes": {"sampled": True}})
        if inv["sampled"] and inv["sampled"] < inv["count"]:
            scale = inv["count"] / inv["sampled"]
            inv["open_estimated"], inv["shared_estimated"] = int(round(inv["open"] * scale)), int(round(inv["shared"] * scale))
        inventory[bo.id] = inv
    if buf:
        session.execute(BusinessObjectInstance.__table__.insert(), buf)

    # ---- interfaces, jobs, impacts, complexity
    interfaces = [{"type": "RFC", "name": r.get("RFCDEST"), "target": r.get("RFCHOST"), "purpose": r.get("RFCOPTIONS")} for r in store.rows("RFCDES")]
    interfaces += [{"type": "IDOC", "name": r.get("PARNUM"), "target": r.get("RCVPOR"), "purpose": r.get("MESTYP")} for r in store.rows("EDPP1")]
    jobs = [{"name": r.get("JOBNAME"), "user": r.get("SDLUNAME"), "periodic": r.get("PERIODIC") == "X"} for r in store.rows("TBTCO")]
    custom_tables = sorted(custom)
    s4_impacts = [{"table": t, "status": TABLES[t].s4_status, "note": TABLES[t].s4_note, "rows": int(sized[t]["rows"])} for t in sized if t in TABLES and TABLES[t].s4_status != "RETAINED"]
    try:
        cross_company_docs = client.count("BKPF", [predicate("BVORG", "NE", "")]) if "BKPF" in sized else 0
    except RfcError:
        cross_company_docs = 0
    shared = sum(i.get("shared_estimated", i["shared"]) for i in inventory.values())
    open_docs = sum(i.get("open_estimated", i["open"]) for i in inventory.values() if i["kind"] == "TRANSACTIONAL")
    complexity = _complexity_from({"company_codes": org_counts["COMPANY_CODE"], "plants": org_counts["PLANT"], "shared_master_data": shared, "cross_company_documents": cross_company_docs, "open_documents": open_docs, "interfaces": len(interfaces), "custom_tables": len(custom_tables)})
    read.update({"snapshot": client.snapshot, "rfc_calls": client.calls, "packages": client.packages, "rows_read": client.rows, "tables_sized": len(sized), "custom_tables": len(custom_tables)})
    snap.summary = {
        "system": _system_facts(system),
        "org_units": dict(org_counts),
        "tables": {"count": len(table_summary), "custom": len(custom_tables), "total_rows": sum(t["rows"] for t in table_summary), "est_bytes": total_bytes, "top": sorted(table_summary, key=lambda t: -t["rows"])[:15]},
        "business_objects": inventory,
        "interfaces": interfaces,
        "jobs": jobs,
        "custom_tables": custom_tables,
        "custom_table_texts": custom,
        "s4_impacts": s4_impacts,
        "complexity": complexity,
        "estimates": _estimates(total_bytes, inventory),
        "read": read,
    }
    snap.status = "COMPLETE"
    session.flush()
    return snap


def _add_rows(store: RecordStore, table: str, rows: list[dict]) -> None:
    for r in rows:
        store._tables[table].append(r)
        store._by_key[table][record_key(table, r)] = r
    for attr in list(vars(store)):
        if "index" in attr:  # the lookup indexes are built lazily: drop the stale ones
            setattr(store, attr, type(getattr(store, attr))())


def _prefetch(client: AbapAddonClient, store: RecordStore, bo_id: str, sample: list[dict], read: dict) -> None:
    """The dependent rows of the sampled instances (items, company-code segments, document flow), read by key;
    where an aggregate answers the question (which company codes a material document touches) only the
    aggregate is read. Line-item tables (BSEG, MSEG) are never read for the inventory."""
    if bo_id in LEAN_NOTES:
        read.setdefault("lean", {})[bo_id] = LEAN_NOTES[bo_id]
    for table, lookup_field, header_field, group_by in AGGREGATE_DEPENDENCIES.get(bo_id, []):
        values = sorted({str(r[header_field]) for r in sample if r.get(header_field)})
        if not values:
            continue
        groups: list[dict] = []
        try:
            for chunk in _chunks(values, 50):
                groups.extend(client.aggregate(table, [predicate(lookup_field, "EQ", v) for v in chunk], group_by, []))
        except RfcError as e:
            read["notes"].append(f"{bo_id}: {table} not aggregated for the sample ({e.key})")
            continue
        _add_rows(store, table, [{**{g: a.get(g) for g in group_by}, "COUNT": a.get("COUNT")} for a in groups])
    for table, lookup_field, header_field in SAMPLE_DEPENDENCIES.get(bo_id, []):
        values = sorted({str(r[header_field]) for r in sample if r.get(header_field)})
        if not values:
            continue
        fetched: list[dict] = []
        try:
            for chunk in _chunks(values, 50):
                fetched.extend(client.read_all(table, [predicate(lookup_field, "EQ", v) for v in chunk]))
        except RfcError as e:
            read["notes"].append(f"{bo_id}: {table} not read for the sample ({e.key})")
            continue
        _add_rows(store, table, fetched)
        if bo_id == "SD.Delivery" and table == "LIPS":
            orders = sorted({str(x["VGBEL"]) for x in fetched if x.get("VGBEL")})
            try:
                heads: list[dict] = []
                for chunk in _chunks(orders, 50):
                    heads.extend(client.read_all("VBAK", [predicate("VBELN", "EQ", v) for v in chunk]))
                _add_rows(store, "VBAK", heads)
            except RfcError as e:
                read["notes"].append(f"{bo_id}: VBAK not read for the sample ({e.key})")


def _system_facts(system: SapSystem) -> dict:
    return {"sid": system.sid, "client": system.client, "product": system.product, "release": system.release, "database": system.database, "os": system.os_name, "logical_system": system.logical_system, "connector": system.connector, "connector_status": system.connector_status}

"""Migration object lookup per S/4HANA release.

The migration cockpit (*Migrate Your Data* app, formerly LTMC) offers a list of migration objects per release.
The platform needs, for every business object it routes to the cockpit, the migration object the files are for.
Two sources feed the lookup:

1. **The catalogue below** (`MIGRATION_OBJECTS`): the migration objects SAP documents for the on-premise releases
   ("Migration Objects for SAP S/4HANA", SAP Help) and the cloud edition, with their *documented names*, the
   release they were introduced in and their renames (Vendor -> Supplier, Material -> Product, "FI - G/L account
   balance" -> "FI - G/L account balance and open item"). The *technical IDs* (`SIF_*` in the on-premise object
   list and in the Migration Object Modeler) are given as hints with `id_confidence = "unverified"`: no system
   was available to read them from, and they differ between releases and editions. The names are what the app
   shows and what the documentation lists; the IDs must be confirmed in the target.
2. **A project registry** imported from the target (the app's object list, exported or copied, or a release's
   documentation): entries with the release, the name and the ID as the system shows them. Registry entries win
   over the catalogue for that release.

`lookup(object_type, release, table, registry)` returns the migration object for a business object with its
provenance (`source`, `confidence`), or the candidates when several apply (accounting documents: receivable,
payable or G/L open items by table), or `none` with the reason (no standard migration object: custom object).
Release strings are normalised (`"2023"`, `"S/4HANA 2023"`, `"2023 FPS02"` -> 2023; `"cloud"`, `"CE"`,
`"2402"`-style cloud releases -> CLOUD).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CLOUD = "CLOUD"
ON_PREMISE_RELEASES = ("1610", "1709", "1809", "1909", "2020", "2021", "2022", "2023", "2025")


def normalize_release(release: str | None) -> str:
    """'2023', 'S/4HANA 2023 FPS01', '2023.2' -> '2023'; 'cloud', 'CE 2402', '2402' -> 'CLOUD'; unknown -> ''."""
    s = (release or "").strip().upper()
    if not s:
        return ""
    if "CLOUD" in s or re.search(r"\bCE\b", s) or re.fullmatch(r"\d{4}", s) and s[:2] in ("24", "25", "26", "27", "28", "29") and s[2:] in ("02", "05", "08", "11"):
        return CLOUD
    m = re.search(r"(1610|1709|1809|1909|20(?:2[0-9]|3[0-9]))", s)
    return m.group(1) if m else ""


def _ge(a: str, b: str) -> bool:
    """release a >= release b (on-premise ordering)."""
    return ON_PREMISE_RELEASES.index(a) >= ON_PREMISE_RELEASES.index(b) if a in ON_PREMISE_RELEASES and b in ON_PREMISE_RELEASES else False


@dataclass(frozen=True)
class MigrationObject:
    key: str  # stable key of the catalogue entry
    names: tuple[tuple[str, str], ...]  # ((from_release, documented name), ...) in release order; the last applicable wins
    object_types: tuple[str, ...]  # business objects it serves
    tables: tuple[str, ...] = ()  # staging tables (for objects with several candidates)
    id_hint: str = ""  # technical ID as commonly shown in the on-premise object list (unverified)
    cloud_name: str = ""  # name in the cloud edition ("" = same as on-premise)
    introduced: str = "1610"
    removed: str | None = None
    approach: str = "file, staging"
    notes: str = ""

    def name_for(self, release: str) -> str | None:
        if release == CLOUD:
            return self.cloud_name or self.names[-1][1]
        if not release:
            return self.names[-1][1]
        if not _ge(release, self.introduced) or (self.removed and _ge(release, self.removed)):
            return None
        name = None
        for frm, n in self.names:
            if _ge(release, frm):
                name = n
        return name


MIGRATION_OBJECTS: tuple[MigrationObject, ...] = (
    MigrationObject("GL_ACCOUNT", (("1610", "G/L account"),), ("FI.GLAccount",), ("SKA1", "SKB1"), "SIF_GL_ACCOUNT", notes="chart of accounts and company code segments"),
    MigrationObject("COST_CENTER", (("1610", "Cost center"),), ("CO.CostCenter",), ("CSKS",), "SIF_COSTCENTER"),
    MigrationObject("PROFIT_CENTER", (("1610", "Profit center"),), ("CO.ProfitCenter",), ("CEPC",), "SIF_PROFITCENTER"),
    MigrationObject("CUSTOMER", (("1610", "Customer"),), ("MD.Customer",), ("KNA1", "KNB1"), "SIF_CUSTOMER", notes="business partner with customer role; CVI applies"),
    MigrationObject("SUPPLIER", (("1610", "Vendor"), ("1709", "Supplier")), ("MD.Vendor",), ("LFA1", "LFB1"), "SIF_SUPPLIER", notes="named Vendor up to 1610; business partner with supplier role"),
    MigrationObject("PRODUCT", (("1610", "Material"), ("2021", "Product")), ("MD.Material",), ("MARA", "MARC", "MBEW", "MARD"), "SIF_MATERIAL", notes="named Material up to 2020; verify the rename release in the target's object list"),
    MigrationObject("BANK", (("1610", "Bank"),), (), (), "SIF_BANK"),
    MigrationObject("FI_GL_OPEN_ITEM", (("1610", "FI - G/L account balance"), ("1709", "FI - G/L account balance and open item")), ("FI.AccountingDocument",), ("BKPF", "BSEG"), "SIF_FI_GL_BALANCE", notes="G/L balances and open items; postings as a whole are not re-created (journal entry API instead)"),
    MigrationObject("FI_AR_OPEN_ITEM", (("1610", "FI - Accounts receivable open item"),), ("FI.AccountingDocument",), ("BSID",), "SIF_FI_AR_OPEN_ITEM", notes="customer open items"),
    MigrationObject("FI_AP_OPEN_ITEM", (("1610", "FI - Accounts payable open item"),), ("FI.AccountingDocument", "MM.InvoiceReceipt"), ("BSIK", "RBKP", "RSEG"), "SIF_FI_AP_OPEN_ITEM", notes="supplier open items; open logistics invoices migrate as payable open items, not as invoice documents"),
    MigrationObject("FIXED_ASSET", (("1610", "Fixed asset (incl. balances)"),), ("FI.FixedAsset",), ("ANLA", "ANLC"), "SIF_FIXED_ASSET", notes="new Asset Accounting; values via the migration object, not ANLC"),
    MigrationObject("INVENTORY_BALANCE", (("1610", "Material inventory balance"), ("1809", "Material - inventory balance")), ("MM.MaterialDocument",), ("MKPF", "MSEG", "MARD"), "SIF_INVENTORY", notes="stock on hand only; historical goods movements are not migrated"),
    MigrationObject("SALES_ORDER_OPEN", (("1709", "Sales order (open)"),), ("SD.SalesOrder",), ("VBAK", "VBAP"), "SIF_SALES_ORDER", introduced="1709", notes="open orders only; the platform re-creates them through API_SALES_ORDER_SRV and uses the cockpit for histories, which this object does not cover"),
    MigrationObject("PURCHASE_CONTRACT", (("1709", "Purchase contract"),), ("MM.Contract",), ("EKKO", "EKPO"), "SIF_PURCHASE_CONTRACT", introduced="1709", notes="contract header and items; release orders are purchase orders referencing it"),
    MigrationObject("PURCHASE_SCHEDULING_AGREEMENT", (("1809", "Purchase scheduling agreement"),), ("MM.SchedulingAgreement",), ("EKKO", "EKPO", "EKET"), "SIF_PURCHASE_SCHED_AGRMT", introduced="1809", notes="agreement with its delivery schedule lines; receipts against it (EKBE) are history"),
    MigrationObject("PURCHASE_ORDER_OPEN", (("1709", "Purchase order (open)"),), ("MM.PurchaseOrder",), ("EKKO", "EKPO"), "SIF_PURCHASE_ORDER", introduced="1709", notes="open orders only; PO history (EKBE) is not covered"),
    MigrationObject("ACTIVITY_TYPE", (("1610", "Activity type"),), (), (), "SIF_ACTIVITY_TYPE"),
    MigrationObject("INTERNAL_ORDER", (("1610", "Internal order"),), (), (), "SIF_INTERNAL_ORDER"),
    MigrationObject("PURCHASING_INFO_RECORD", (("1610", "Purchasing info record"),), (), (), "SIF_PIR"),
    MigrationObject("SOURCE_LIST", (("1709", "Source list"),), (), (), "SIF_SOURCE_LIST", introduced="1709"),
    MigrationObject("BATCH", (("1610", "Batch"),), ("MD.Batch",), ("MCH1", "MCHA", "MCHB"), "SIF_BATCH", notes="batch master unique at material level; batch stock quantities belong to the inventory balance object"),
    MigrationObject("BILL_OF_MATERIAL", (("1610", "Bill of material"),), ("MD.BillOfMaterial",), ("MAST", "STKO", "STPO"), "SIF_BOM", notes="material BOM per plant and usage"),
    MigrationObject("ROUTING", (("1610", "Routing"),), ("MD.Routing",), ("MAPL", "PLKO", "PLPO"), "SIF_ROUTING", notes="task list with operations; work centers first"),
    MigrationObject("WORK_CENTER", (("1610", "Work center"),), ("MD.WorkCenter",), ("CRHD", "CRCO"), "SIF_WORK_CENTER", notes="with the cost center assignment"),
    MigrationObject("PRICING_CONDITION", (("1610", "Condition record for pricing (general template)"),), (), (), "SIF_PRICING_CONDITION"),
    MigrationObject("EQUIPMENT", (("1610", "Equipment"),), (), (), "SIF_EQUIPMENT"),
    MigrationObject("FUNCTIONAL_LOCATION", (("1610", "Functional location"),), (), (), "SIF_FUNC_LOCATION"),
    MigrationObject("EXCHANGE_RATE", (("1610", "Exchange rate"),), (), (), "SIF_EXCHANGE_RATE"),
)
NO_STANDARD_OBJECT: dict[str, str] = {
    "SD.BillingDocument": "no standard migration object for historical billing documents: a custom migration object (Migration Object Modeler) or an archive-like history table",
    "SD.Delivery": "no standard migration object for goods-issued deliveries: history",
    "PP.ProductionOrder": "no standard migration object for production orders with components and confirmations: custom migration object",
    "Z.ExportControl": "custom table: custom migration object (Migration Object Modeler)",
    "Z.TsaScope": "custom table: custom migration object (Migration Object Modeler)",
    "Z.SupplierExt": "custom table: custom migration object (Migration Object Modeler)",
}
CATALOGUE_SOURCE = "SAP Help, 'Migration Objects for SAP S/4HANA' per release (names); technical IDs as commonly shown in the on-premise object list, unverified here"


def catalogue(release: str | None = None) -> list[dict]:
    rel = normalize_release(release) if release else ""
    out = []
    for mo in MIGRATION_OBJECTS:
        name = mo.name_for(rel)
        out.append({"key": mo.key, "name": name, "available": name is not None, "names": [{"from_release": f, "name": n} for f, n in mo.names], "cloud_name": mo.cloud_name or mo.names[-1][1], "object_types": list(mo.object_types), "tables": list(mo.tables), "id_hint": mo.id_hint, "id_confidence": "unverified", "introduced": mo.introduced, "removed": mo.removed, "approach": mo.approach, "notes": mo.notes})
    return out


def lookup(object_type: str, release: str | None, table: str | None = None, registry: list[dict] | None = None) -> dict:
    """Resolve the migration object of a business object for a release.

    `registry`: project entries `{release, name, id, object_types, tables, notes, source}` (release "*" = any).
    Returns `{status: found|candidates|none, name, id, release, source, confidence, candidates, note}`."""
    rel = normalize_release(release)
    rel_label = rel or (release or "")
    # 1. project registry
    for e in registry or []:
        e_rel = normalize_release(e.get("release")) if e.get("release") not in (None, "", "*") else "*"
        if e_rel not in ("*", rel):
            continue
        if object_type in (e.get("object_types") or []) and (not table or not e.get("tables") or table in e.get("tables")):
            return {"status": "found", "name": e.get("name", ""), "id": e.get("id", ""), "release": rel_label, "source": e.get("source") or "project registry", "confidence": "imported from the target" if e.get("id") else "imported name, no ID", "candidates": [], "note": e.get("notes", "")}
    # 2. catalogue
    cands = []
    for mo in MIGRATION_OBJECTS:
        if object_type not in mo.object_types:
            continue
        name = mo.name_for(rel)
        if name is None:
            continue
        hit = {"key": mo.key, "name": name, "id": mo.id_hint, "tables": list(mo.tables), "notes": mo.notes, "table_match": bool(table and table in mo.tables)}
        cands.append(hit)
    if not cands:
        if object_type in NO_STANDARD_OBJECT:
            return {"status": "none", "name": "", "id": "", "release": rel_label, "source": "catalogue", "confidence": "documented", "candidates": [], "note": NO_STANDARD_OBJECT[object_type]}
        unavailable = [mo for mo in MIGRATION_OBJECTS if object_type in mo.object_types]
        note = f"{unavailable[0].names[-1][1]} is not available in release {rel_label} (introduced in {unavailable[0].introduced})" if unavailable and rel else "no migration object known for this business object; import the target's object list"
        return {"status": "none", "name": "", "id": "", "release": rel_label, "source": "catalogue", "confidence": "documented" if unavailable else "unknown", "candidates": [], "note": note}
    chosen = next((c for c in cands if c["table_match"]), cands[0] if len(cands) == 1 else None)
    if chosen is None:
        return {"status": "candidates", "name": "", "id": "", "release": rel_label, "source": "catalogue", "confidence": "documented name, ID unverified", "candidates": cands, "note": "several migration objects apply; the staging table decides (" + ", ".join(f"{c['name']}: {', '.join(c['tables'])}" for c in cands) + ")"}
    return {"status": "found", "name": chosen["name"], "id": chosen["id"], "release": rel_label, "source": "catalogue", "confidence": "documented name, ID unverified" + (" (release unknown: latest name)" if not rel else ""), "candidates": cands if len(cands) > 1 else [], "note": chosen["notes"]}


def lookup_table(object_type: str, release: str | None, registry: list[dict] | None = None) -> dict:
    """The lookup for every business object the catalogue or the registry knows (plus the 'no standard object' ones)."""
    types = sorted({t for mo in MIGRATION_OBJECTS for t in mo.object_types} | set(NO_STANDARD_OBJECT) | {t for e in registry or [] for t in e.get("object_types") or []})
    return {t: lookup(t, release, None, registry) for t in types if not object_type or t == object_type}


__all__ = ["CLOUD", "ON_PREMISE_RELEASES", "MIGRATION_OBJECTS", "NO_STANDARD_OBJECT", "CATALOGUE_SOURCE", "normalize_release", "catalogue", "lookup", "lookup_table", "MigrationObject", "field"]

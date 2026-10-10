"""Canonical SAP business object model and semantic relationships.

This is the semantic layer that table-level filters lack: each business object type knows its leading table,
how to derive its key, its organisational owner, its lifecycle status, and how it is related to other
objects. Relationship resolvers are explicit functions, not DDIC foreign-key inference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .store import RecordStore


@dataclass(frozen=True)
class LoadMethod:
    target: str  # S4HANA | ECC
    method: str  # API | BAPI | MIGRATION_COCKPIT | DIRECT_TABLE_UNSUPPORTED | CONFIG_TRANSPORT
    api: str = ""
    note: str = ""


@dataclass(frozen=True)
class BusinessObjectType:
    id: str
    name: str
    domain: str  # FI, CO, SD, MM, PP, FI-AA, MD, CONFIG, BASIS
    kind: str  # MASTER | TRANSACTIONAL | CONFIG | TECHNICAL
    header_table: str
    item_tables: tuple[str, ...] = ()
    key_fields: tuple[str, ...] = ()
    org_scope: str = "COMPANY_CODE"  # COMPANY_CODE | PLANT | CLIENT | CONTROLLING_AREA
    org_field: str | None = "BUKRS"
    year_field: str | None = None
    applicability: tuple[str, ...] = ("ECC", "S4HANA")
    load_methods: tuple[LoadMethod, ...] = ()
    s4_simplification: str = ""
    description: str = ""

    def key_of(self, row: dict) -> str:
        return "|".join(str(row.get(k, "")) for k in (self.key_fields or ()))


# ---------------------------------------------------------------------------------- registry
_S4_BP = LoadMethod("S4HANA", "API", "API_BUSINESS_PARTNER (released OData)", "Customer/Vendor are loaded as Business Partner roles; CVI mapping applied")
_S4_MC = LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Migration Cockpit / Staging tables", "Open items and balances via released migration objects")
_S4_API_SO = LoadMethod("S4HANA", "API", "API_SALES_ORDER_SRV", "Open sales documents re-created through released API")
_S4_API_PO = LoadMethod("S4HANA", "API", "API_PURCHASEORDER_PROCESS_SRV", "Open purchase documents re-created through released API")
_S4_API_MAT = LoadMethod("S4HANA", "API", "API_PRODUCT_SRV", "Products loaded via released API; MATNR length check")
_S4_JOURNAL = LoadMethod("S4HANA", "API", "API_JOURNALENTRY_SRV (SOAP)", "Historical documents posted as journal entries into Universal Journal; no direct ACDOCA writes")
_S4_FAA = LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Fixed asset incl. balances (migration object)", "New Asset Accounting; values via migration object, not ANLC")
_CFG = LoadMethod("S4HANA", "CONFIG_TRANSPORT", "Customizing transport / configuration shell", "Org structures come from the configuration shell, data is matched not loaded")
_UNSUPPORTED = LoadMethod("S4HANA", "DIRECT_TABLE_UNSUPPORTED", "", "Direct writes to application tables are not performed by this platform")

BUSINESS_OBJECTS: dict[str, BusinessObjectType] = {
    bo.id: bo
    for bo in [
        BusinessObjectType("CFG.CompanyCode", "Company code", "CONFIG", "CONFIG", "T001", (), ("BUKRS",), "COMPANY_CODE", "BUKRS", load_methods=(_CFG,)),
        BusinessObjectType("CFG.Plant", "Plant", "CONFIG", "CONFIG", "T001W", ("T001K",), ("WERKS",), "PLANT", "WERKS", load_methods=(_CFG,)),
        BusinessObjectType("CFG.SalesOrg", "Sales organisation", "CONFIG", "CONFIG", "TVKO", (), ("VKORG",), "COMPANY_CODE", "BUKRS", load_methods=(_CFG,)),
        BusinessObjectType("CFG.PurchOrg", "Purchasing organisation", "CONFIG", "CONFIG", "T024E", (), ("EKORG",), "COMPANY_CODE", "BUKRS", load_methods=(_CFG,)),
        BusinessObjectType("CFG.ControllingArea", "Controlling area", "CONFIG", "CONFIG", "TKA01", ("TKA02",), ("KOKRS",), "CLIENT", None, load_methods=(_CFG,)),
        BusinessObjectType("FI.GLAccount", "G/L account", "FI", "MASTER", "SKB1", ("SKA1",), ("BUKRS", "SAKNR"), "COMPANY_CODE", "BUKRS", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "G/L account (migration object)"),), s4_simplification="Chart-of-accounts harmonisation applied through value mapping"),
        BusinessObjectType("CO.CostCenter", "Cost center", "CO", "MASTER", "CSKS", (), ("KOKRS", "KOSTL"), "COMPANY_CODE", "BUKRS", load_methods=(LoadMethod("S4HANA", "API", "API_COSTCENTER_SRV"),)),
        BusinessObjectType("CO.ProfitCenter", "Profit center", "CO", "MASTER", "CEPC", (), ("KOKRS", "PRCTR"), "COMPANY_CODE", "BUKRS", load_methods=(LoadMethod("S4HANA", "API", "API_PROFITCENTER_SRV"),)),
        BusinessObjectType("MD.Customer", "Customer (Business Partner role)", "MD", "MASTER", "KNA1", ("KNB1",), ("KUNNR",), "CLIENT", None, load_methods=(_S4_BP,), s4_simplification="Customer/Vendor Integration: Business Partner is the leading object"),
        BusinessObjectType("MD.Vendor", "Vendor / Supplier (Business Partner role)", "MD", "MASTER", "LFA1", ("LFB1",), ("LIFNR",), "CLIENT", None, load_methods=(_S4_BP,), s4_simplification="Customer/Vendor Integration: Business Partner is the leading object"),
        BusinessObjectType("MD.Material", "Material / Product", "MD", "MASTER", "MARA", ("MARC", "MBEW", "MARD"), ("MATNR",), "CLIENT", None, load_methods=(_S4_API_MAT,), s4_simplification="MATNR 40 characters; Material Ledger mandatory; MARD stock derived from MATDOC"),
        BusinessObjectType("FI.FixedAsset", "Fixed asset", "FI-AA", "MASTER", "ANLA", ("ANLC",), ("BUKRS", "ANLN1", "ANLN2"), "COMPANY_CODE", "BUKRS", load_methods=(_S4_FAA,), s4_simplification="New Asset Accounting mandatory; ANLC replaced by ACDOCA"),
        BusinessObjectType("SD.SalesOrder", "Sales order", "SD", "TRANSACTIONAL", "VBAK", ("VBAP",), ("VBELN",), "COMPANY_CODE", "BUKRS_VF", "AUDAT", load_methods=(_S4_API_SO,), s4_simplification="Credit management (FSCM) and condition technique changes"),
        BusinessObjectType("SD.Delivery", "Outbound delivery", "SD", "TRANSACTIONAL", "LIKP", ("LIPS",), ("VBELN",), "COMPANY_CODE", "BUKRS", "WADAT_IST", load_methods=(LoadMethod("S4HANA", "API", "API_OUTBOUND_DELIVERY_SRV", "Only open deliveries are re-created; closed ones are history"),)),
        BusinessObjectType("SD.BillingDocument", "Billing document", "SD", "TRANSACTIONAL", "VBRK", ("VBRP",), ("VBELN",), "COMPANY_CODE", "BUKRS", "GJAHR", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Historical billing as archive-like history (planned)", "Billing documents are not re-posted; FI effects migrate via journal entries"),)),
        BusinessObjectType("MM.PurchaseOrder", "Purchase order", "MM", "TRANSACTIONAL", "EKKO", ("EKPO", "EKBE"), ("EBELN",), "COMPANY_CODE", "BUKRS", "GJAHR", load_methods=(_S4_API_PO,)),
        BusinessObjectType("MM.MaterialDocument", "Material document (goods movement)", "MM", "TRANSACTIONAL", "MKPF", ("MSEG",), ("MBLNR", "MJAHR"), "COMPANY_CODE", "BUKRS", "MJAHR", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Stock balances via migration object; historical movements as history", "MATDOC is leading in S/4HANA"),), s4_simplification="MATDOC replaces MKPF/MSEG as leading table"),
        BusinessObjectType("MM.InvoiceReceipt", "Logistics invoice", "MM", "TRANSACTIONAL", "RBKP", ("RSEG",), ("BELNR", "GJAHR"), "COMPANY_CODE", "BUKRS", "GJAHR", load_methods=(_S4_MC,)),
        BusinessObjectType("FI.AccountingDocument", "Accounting document", "FI", "TRANSACTIONAL", "BKPF", ("BSEG", "BSID", "BSIK"), ("BUKRS", "BELNR", "GJAHR"), "COMPANY_CODE", "BUKRS", "GJAHR", load_methods=(_S4_JOURNAL, _S4_MC), s4_simplification="Universal Journal (ACDOCA); open items via migration objects, history via journal entry API"),
        BusinessObjectType("PP.ProductionOrder", "Production order", "PP", "TRANSACTIONAL", "AFKO", ("AFPO", "AUFK", "AFRU"), ("AUFNR",), "PLANT", "DWERK", load_methods=(LoadMethod("S4HANA", "API", "API_PRODUCTION_ORDER_2_SRV", "Only open orders re-created"),)),
        BusinessObjectType("MD.BillOfMaterial", "Bill of material", "PP", "MASTER", "STKO", ("MAST", "STPO"), ("STLNR", "STLAL"), "PLANT", None, load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Bill of material (migration object)", "material BOM per plant and usage; components must exist as products in the target"),), description="material BOM: the BOM header and alternative with its items and its material-plant-usage assignments (MAST); plant-scoped through the assignments"),
        BusinessObjectType("MD.Routing", "Routing", "PP", "MASTER", "PLKO", ("MAPL", "PLPO"), ("PLNNR", "PLNTY", "PLNAL"), "PLANT", "WERKS", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Routing (migration object)", "task list with operations; work centers must exist in the target"),), description="routing: the task list header and group counter with its operations and its material-plant assignments (MAPL)"),
        BusinessObjectType("MD.WorkCenter", "Work center", "PP", "MASTER", "CRHD", ("CRCO",), ("OBJID",), "PLANT", "WERKS", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Work center (migration object)", "with the cost center assignment; the cost center must exist in the target"),), description="work center with its cost center assignment"),
        BusinessObjectType("MD.Batch", "Batch", "MM", "MASTER", "MCH1", ("MCHA", "MCHB"), ("CHARG", "MATNR"), "PLANT", None, load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Batch (migration object)", "batch master; batch stock quantities through the inventory balance object"),), description="batch of a material (unique at material level) with its plant batches and batch stock; plant-scoped through the plant batches"),
        BusinessObjectType("BASIS.RfcDestination", "RFC destination", "BASIS", "TECHNICAL", "RFCDES", (), ("RFCDEST",), "CLIENT", None, load_methods=(_UNSUPPORTED,)),
        BusinessObjectType("BASIS.IdocPartner", "IDoc partner profile", "BASIS", "TECHNICAL", "EDPP1", (), ("PARNUM", "PARTYP"), "CLIENT", None, load_methods=(_UNSUPPORTED,)),
        BusinessObjectType("BASIS.BackgroundJob", "Background job", "BASIS", "TECHNICAL", "TBTCO", (), ("JOBNAME", "JOBCOUNT"), "CLIENT", None, load_methods=(_UNSUPPORTED,)),
        BusinessObjectType("Z.ExportControl", "Custom export-control classification", "SD", "MASTER", "ZSD_EXPORT_CTRL", (), ("MATNR",), "CLIENT", None, load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Custom table via custom migration object"),)),
        BusinessObjectType("Z.TsaScope", "Custom TSA scope", "FI", "CONFIG", "ZFI_TSA_SCOPE", (), ("BUKRS", "TSA_ID"), "COMPANY_CODE", "BUKRS", load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Custom table via custom migration object"),)),
        BusinessObjectType("Z.SupplierExt", "Custom supplier extension", "MM", "MASTER", "ZMM_SUPPLIER_EXT", (), ("LIFNR",), "CLIENT", None, load_methods=(LoadMethod("S4HANA", "MIGRATION_COCKPIT", "Custom table via custom migration object"),)),
    ]
}


def bo(id_: str) -> BusinessObjectType:
    return BUSINESS_OBJECTS[id_]


# ------------------------------------------------------------------------- instance derivation

def _plants_of(t: str, row: dict, store: RecordStore) -> list[str]:
    """Plants a BOM, routing or batch is assigned in (MAST / MAPL / MCHA), owner first, duplicates removed."""
    if t == "MD.BillOfMaterial":
        plants = [m["WERKS"] for m in store.lookup("MAST", "STLNR", row["STLNR"]) if str(m["STLAL"]) == str(row["STLAL"])]
    elif t == "MD.Routing":
        plants = ([row["WERKS"]] if row.get("WERKS") else []) + [m["WERKS"] for m in store.lookup("MAPL", "PLNNR", row["PLNNR"]) if m["PLNTY"] == row["PLNTY"] and str(m["PLNAL"]) == str(row["PLNAL"])]
    else:
        plants = [b["WERKS"] for b in store.lookup("MCHA", "CHARG", row["CHARG"]) if b["MATNR"] == row["MATNR"]]
    return list(dict.fromkeys(p for p in plants if p))

def instance_company_codes(bo_type: BusinessObjectType, row: dict, store: RecordStore) -> list[str]:
    """All company codes an instance touches (owner first). Used for shared / cross-company detection."""
    ccs: list[str] = []

    def add(c):
        if c and c not in ccs:
            ccs.append(c)

    t = bo_type.id
    if t == "CFG.Plant":
        k = store.get("T001K", BWKEY=row["WERKS"])
        add(k["BUKRS"] if k else None)
    elif t == "CFG.ControllingArea":
        for a in store.lookup("TKA02", "KOKRS", row["KOKRS"]):
            add(a["BUKRS"])
    elif t == "MD.Customer":
        for r in store.lookup("KNB1", "KUNNR", row["KUNNR"]):
            add(r["BUKRS"])
    elif t == "MD.Vendor":
        for r in store.lookup("LFB1", "LIFNR", row["LIFNR"]):
            add(r["BUKRS"])
    elif t == "MD.Material":
        for r in store.lookup("MARC", "MATNR", row["MATNR"]):
            k = store.get("T001K", BWKEY=r["WERKS"])
            add(k["BUKRS"] if k else None)
    elif t == "CO.CostCenter" or t == "CO.ProfitCenter":
        add(row.get("BUKRS"))
    elif t == "SD.SalesOrder":
        add(row.get("BUKRS_VF"))
        for it in store.lookup("VBAP", "VBELN", row["VBELN"]):
            k = store.get("T001K", BWKEY=it["WERKS"])
            add(k["BUKRS"] if k else None)
    elif t == "SD.Delivery":
        add(row.get("BUKRS"))
        so_keys = {it["VGBEL"] for it in store.lookup("LIPS", "VBELN", row["VBELN"])}
        for so in so_keys:
            h = store.get("VBAK", VBELN=so)
            add(h["BUKRS_VF"] if h else None)
    elif t == "SD.BillingDocument":
        add(row.get("BUKRS"))
        for it in store.lookup("VBRP", "VBELN", row["VBELN"]):
            k = store.get("T001K", BWKEY=it["WERKS"])
            add(k["BUKRS"] if k else None)
    elif t == "MM.PurchaseOrder":
        add(row.get("BUKRS"))
        for it in store.lookup("EKPO", "EBELN", row["EBELN"]):
            k = store.get("T001K", BWKEY=it["WERKS"])
            add(k["BUKRS"] if k else None)
    elif t == "MM.MaterialDocument":
        for it in store.lookup("MSEG", "MBLNR", row["MBLNR"]):
            if str(it.get("MJAHR")) == str(row["MJAHR"]):
                add(it.get("BUKRS"))
    elif t == "MM.InvoiceReceipt":
        add(row.get("BUKRS"))
    elif t == "FI.AccountingDocument":
        add(row.get("BUKRS"))
        if row.get("BVORG"):
            for h in store.lookup("BKPF", "BVORG", row["BVORG"]):
                add(h["BUKRS"])
    elif t == "PP.ProductionOrder":
        k = store.get("T001K", BWKEY=row["DWERK"])
        add(k["BUKRS"] if k else None)
    elif t == "MD.WorkCenter":
        k = store.get("T001K", BWKEY=row["WERKS"])
        add(k["BUKRS"] if k else None)
    elif t in ("MD.BillOfMaterial", "MD.Routing", "MD.Batch"):
        for werks in _plants_of(t, row, store):
            k = store.get("T001K", BWKEY=werks)
            add(k["BUKRS"] if k else None)
    elif t == "Z.ExportControl":
        for r in store.lookup("MARC", "MATNR", row["MATNR"]):
            k = store.get("T001K", BWKEY=r["WERKS"])
            add(k["BUKRS"] if k else None)
    elif t == "Z.SupplierExt":
        for r in store.lookup("LFB1", "LIFNR", row["LIFNR"]):
            add(r["BUKRS"])
    elif bo_type.org_field:
        add(row.get(bo_type.org_field))
    return ccs


def instance_status(bo_type: BusinessObjectType, row: dict, store: RecordStore) -> str | None:
    t = bo_type.id
    if t == "SD.SalesOrder":
        return "CLOSED" if row.get("GBSTK") == "C" else "OPEN"
    if t == "SD.Delivery":
        return "CLOSED" if any(f["VBTYP_N"] == "M" for f in store.lookup("VBFA", "VBELV", row["VBELN"])) else "OPEN"
    if t == "SD.BillingDocument":
        return "CLOSED" if row.get("RFBSK") == "C" else "OPEN"
    if t == "MM.PurchaseOrder":
        items = store.lookup("EKPO", "EBELN", row["EBELN"])
        invoiced = {h["EBELP"] for h in store.lookup("EKBE", "EBELN", row["EBELN"]) if h["VGABE"] == "2"}
        return "CLOSED" if items and all(i["ELIKZ"] == "X" and i["EBELP"] in invoiced for i in items) else "OPEN"
    if t == "MM.InvoiceReceipt":
        return "CLOSED" if row.get("RBSTAT") == "5" else "OPEN"
    if t == "FI.AccountingDocument":
        if store.count("BSEG"):
            lines = [l for l in store.lookup("BSEG", "BELNR", row["BELNR"]) if l["BUKRS"] == row["BUKRS"] and str(l["GJAHR"]) == str(row["GJAHR"])]
            open_items = [l for l in lines if l["KOART"] in ("D", "K") and not l.get("AUGBL")]
            return "OPEN" if open_items else "CLOSED"
        # without the line items: the open-item tables (BSID customers, BSIK vendors) answer the same question
        for oi in ("BSID", "BSIK"):
            if any(l["BUKRS"] == row["BUKRS"] and str(l["GJAHR"]) == str(row["GJAHR"]) and not l.get("AUGBL") for l in store.lookup(oi, "BELNR", row["BELNR"])):
                return "OPEN"
        return "CLOSED"
    if t == "PP.ProductionOrder":
        item = store.get("AFPO", AUFNR=row["AUFNR"], POSNR=1)
        return "CLOSED" if item and item["WEMNG"] >= item["PSMNG"] else "OPEN"
    return None


# ------------------------------------------------------------------------- relationships
@dataclass(frozen=True)
class Relationship:
    from_type: str
    to_type: str
    edge_type: str  # DOC_FLOW | ACCOUNTING_REF | MASTER_REF | ORG_OWNERSHIP | PARENT_CHILD | CROSS_COMPANY | SHARED
    name: str
    resolver: Callable[[dict, RecordStore], list[str]]  # returns target object keys
    description: str = ""


def _vbfa_targets(vbtyp: str):
    def f(row, store):
        return sorted({x["VBELN"] for x in store.lookup("VBFA", "VBELV", row["VBELN"]) if x["VBTYP_N"] == vbtyp})
    return f


def _vbfa_material_docs(row, store):
    out = set()
    for x in store.lookup("VBFA", "VBELV", row["VBELN"]):
        if x["VBTYP_N"] == "R":
            h = store.lookup("MKPF", "MBLNR", x["VBELN"])
            for m in h:
                out.add(f"{m['MBLNR']}|{m['MJAHR']}")
    return sorted(out)


def _accounting_for(awtyp: str, key_fn):
    def f(row, store):
        awkey = key_fn(row)
        return sorted(f"{h['BUKRS']}|{h['BELNR']}|{h['GJAHR']}" for h in store.lookup("BKPF", "AWKEY", awkey) if h["AWTYP"] == awtyp)
    return f


def _clearing_docs(row, store):
    out = set()
    for l in store.lookup("BSEG", "BELNR", row["BELNR"]):
        if l["BUKRS"] == row["BUKRS"] and l.get("AUGBL") and l["AUGBL"] != row["BELNR"]:
            out.add(f"{row['BUKRS']}|{l['AUGBL']}|{row['GJAHR']}")
    return sorted(out)


def _cross_company_docs(row, store):
    if not row.get("BVORG"):
        return []
    return sorted(f"{h['BUKRS']}|{h['BELNR']}|{h['GJAHR']}" for h in store.lookup("BKPF", "BVORG", row["BVORG"]) if h["BELNR"] != row["BELNR"])


def _bseg_partners(koart: str, fld: str):
    def f(row, store):
        return sorted({l[fld] for l in store.lookup("BSEG", "BELNR", row["BELNR"]) if l["BUKRS"] == row["BUKRS"] and l["KOART"] == koart and l.get(fld)})
    return f


def _bseg_field(fld: str, prefix_kokrs: bool = False):
    def f(row, store):
        vals = {l[fld] for l in store.lookup("BSEG", "BELNR", row["BELNR"]) if l["BUKRS"] == row["BUKRS"] and l.get(fld)}
        if prefix_kokrs:
            a = store.get("TKA02", BUKRS=row["BUKRS"])
            kokrs = a["KOKRS"] if a else ""
            return sorted(f"{kokrs}|{v}" for v in vals)
        if fld == "HKONT":
            return sorted(f"{row['BUKRS']}|{v}" for v in vals)
        return sorted(vals)
    return f


def _items_field(item_table: str, parent_field: str, fld: str):
    def f(row, store):
        return sorted({i[fld] for i in store.lookup(item_table, parent_field, row[parent_field]) if i.get(fld)})
    return f


def _plant_owner(field: str):
    def f(row, store):
        k = store.get("T001K", BWKEY=row[field])
        return [k["BUKRS"]] if k else []
    return f


RELATIONSHIPS: list[Relationship] = [
    # --- order to cash
    Relationship("SD.SalesOrder", "SD.Delivery", "DOC_FLOW", "SalesOrder→Delivery", _vbfa_targets("J"), "Document flow (VBFA, subsequent delivery)"),
    Relationship("SD.Delivery", "MM.MaterialDocument", "DOC_FLOW", "Delivery→GoodsIssue", _vbfa_material_docs, "Goods issue material document"),
    Relationship("SD.Delivery", "SD.BillingDocument", "DOC_FLOW", "Delivery→Billing", _vbfa_targets("M"), "Document flow (VBFA, subsequent billing incl. intercompany billing)"),
    Relationship("SD.BillingDocument", "FI.AccountingDocument", "ACCOUNTING_REF", "Billing→AccountingDocument", _accounting_for("VBRK", lambda r: r["VBELN"]), "BKPF reference (AWTYP=VBRK)"),
    Relationship("SD.SalesOrder", "MD.Customer", "MASTER_REF", "SalesOrder→SoldTo", lambda r, s: [r["KUNNR"]]),
    Relationship("SD.SalesOrder", "MD.Material", "MASTER_REF", "SalesOrder→Material", _items_field("VBAP", "VBELN", "MATNR")),
    Relationship("SD.SalesOrder", "CFG.Plant", "ORG_OWNERSHIP", "SalesOrder→DeliveringPlant", _items_field("VBAP", "VBELN", "WERKS"), "Plant of items; a plant outside the selling company code marks a cross-company sale"),
    Relationship("SD.BillingDocument", "MD.Customer", "MASTER_REF", "Billing→Payer", lambda r, s: [r["KUNRG"]]),
    # --- procure to pay
    Relationship("MM.PurchaseOrder", "MM.MaterialDocument", "DOC_FLOW", "PurchaseOrder→GoodsReceipt", lambda r, s: sorted({f"{h['BELNR']}|{h['GJAHR']}" for h in s.lookup("EKBE", "EBELN", r["EBELN"]) if h["VGABE"] == "1"}), "PO history (EKBE, goods receipt)"),
    Relationship("MM.PurchaseOrder", "MM.InvoiceReceipt", "DOC_FLOW", "PurchaseOrder→InvoiceReceipt", lambda r, s: sorted({f"{h['BELNR']}|{h['GJAHR']}" for h in s.lookup("EKBE", "EBELN", r["EBELN"]) if h["VGABE"] == "2"}), "PO history (EKBE, invoice receipt)"),
    Relationship("MM.InvoiceReceipt", "FI.AccountingDocument", "ACCOUNTING_REF", "InvoiceReceipt→AccountingDocument", _accounting_for("RMRP", lambda r: f"{r['BELNR']}{r['GJAHR']}")),
    Relationship("MM.MaterialDocument", "FI.AccountingDocument", "ACCOUNTING_REF", "MaterialDocument→AccountingDocument", _accounting_for("MKPF", lambda r: f"{r['MBLNR']}{r['MJAHR']}")),
    Relationship("MM.PurchaseOrder", "MD.Vendor", "MASTER_REF", "PurchaseOrder→Vendor", lambda r, s: [r["LIFNR"]]),
    Relationship("MM.PurchaseOrder", "MD.Material", "MASTER_REF", "PurchaseOrder→Material", _items_field("EKPO", "EBELN", "MATNR")),
    Relationship("MM.PurchaseOrder", "CFG.Plant", "ORG_OWNERSHIP", "PurchaseOrder→ReceivingPlant", _items_field("EKPO", "EBELN", "WERKS"), "Plant of items; a plant outside the ordering company code marks a cross-company PO"),
    Relationship("MM.MaterialDocument", "MD.Material", "MASTER_REF", "MaterialDocument→Material", lambda r, s: sorted({i["MATNR"] for i in s.lookup("MSEG", "MBLNR", r["MBLNR"]) if str(i["MJAHR"]) == str(r["MJAHR"])})),
    Relationship("MM.MaterialDocument", "CFG.Plant", "ORG_OWNERSHIP", "MaterialDocument→Plant", lambda r, s: sorted({i["WERKS"] for i in s.lookup("MSEG", "MBLNR", r["MBLNR"]) if str(i["MJAHR"]) == str(r["MJAHR"])}), "Two plants in different company codes mark a cross-company stock transfer"),
    # --- make to stock
    Relationship("PP.ProductionOrder", "MM.MaterialDocument", "DOC_FLOW", "ProductionOrder→ComponentConsumption", lambda r, s: sorted({f"{i['MBLNR']}|{i['MJAHR']}" for i in s.lookup("MSEG", "AUFNR", r["AUFNR"])})),
    Relationship("PP.ProductionOrder", "MD.Material", "MASTER_REF", "ProductionOrder→Product", lambda r, s: [r["PLNBEZ"]]),
    Relationship("PP.ProductionOrder", "CO.CostCenter", "MASTER_REF", "ProductionOrder→SettlementCostCenter", lambda r, s: [f"{a['KOKRS']}|{a['KOSTL']}" for a in [s.get("AUFK", AUFNR=r["AUFNR"])] if a]),
    Relationship("PP.ProductionOrder", "MD.BillOfMaterial", "MASTER_REF", "ProductionOrder→BillOfMaterial", lambda r, s: sorted({f"{m['STLNR']}|{m['STLAL']}" for m in s.lookup("MAST", "MATNR", r["PLNBEZ"]) if m["WERKS"] == r["DWERK"]}), "BOM of the product in the producing plant"),
    Relationship("PP.ProductionOrder", "MD.Routing", "MASTER_REF", "ProductionOrder→Routing", lambda r, s: sorted({f"{m['PLNNR']}|{m['PLNTY']}|{m['PLNAL']}" for m in s.lookup("MAPL", "MATNR", r["PLNBEZ"]) if m["WERKS"] == r["DWERK"]}), "Routing of the product in the producing plant"),
    # --- manufacturing masters
    Relationship("MD.Material", "MD.BillOfMaterial", "PARENT_CHILD", "Material→BillOfMaterial", lambda r, s: sorted({f"{m['STLNR']}|{m['STLAL']}" for m in s.lookup("MAST", "MATNR", r["MATNR"])}), "BOMs assigned to the material (per plant and usage)"),
    Relationship("MD.Material", "MD.Routing", "PARENT_CHILD", "Material→Routing", lambda r, s: sorted({f"{m['PLNNR']}|{m['PLNTY']}|{m['PLNAL']}" for m in s.lookup("MAPL", "MATNR", r["MATNR"])}), "Routings assigned to the material (per plant)"),
    Relationship("MD.Material", "MD.Batch", "PARENT_CHILD", "Material→Batch", lambda r, s: [f"{b['CHARG']}|{b['MATNR']}" for b in s.lookup("MCH1", "MATNR", r["MATNR"])], "Batches of the material"),
    Relationship("MD.BillOfMaterial", "MD.Material", "MASTER_REF", "BillOfMaterial→Component", lambda r, s: sorted({i["IDNRK"] for i in s.lookup("STPO", "STLNR", r["STLNR"]) if str(i["STLAL"]) == str(r["STLAL"])}), "Components of the BOM"),
    Relationship("MD.BillOfMaterial", "CFG.Plant", "ORG_OWNERSHIP", "BillOfMaterial→Plant", lambda r, s: _plants_of("MD.BillOfMaterial", r, s), "Plants the BOM is assigned in"),
    Relationship("MD.Routing", "MD.WorkCenter", "MASTER_REF", "Routing→WorkCenter", lambda r, s: sorted({str(o["ARBID"]) for o in s.lookup("PLPO", "PLNNR", r["PLNNR"]) if str(o["PLNAL"]) == str(r["PLNAL"]) and o.get("ARBID")}), "Work centers of the operations"),
    Relationship("MD.Routing", "CFG.Plant", "ORG_OWNERSHIP", "Routing→Plant", lambda r, s: _plants_of("MD.Routing", r, s), "Plant of the task list and the plants it is assigned in"),
    Relationship("MD.WorkCenter", "CFG.Plant", "ORG_OWNERSHIP", "WorkCenter→Plant", lambda r, s: [r["WERKS"]]),
    Relationship("MD.WorkCenter", "CO.CostCenter", "MASTER_REF", "WorkCenter→CostCenter", lambda r, s: sorted({f"{c['KOKRS']}|{c['KOSTL']}" for c in s.lookup("CRCO", "OBJID", r["OBJID"]) if c.get("KOSTL")}), "Cost center the work center's activities settle to"),
    Relationship("MD.Batch", "MD.Material", "MASTER_REF", "Batch→Material", lambda r, s: [r["MATNR"]]),
    Relationship("MD.Batch", "CFG.Plant", "ORG_OWNERSHIP", "Batch→Plant", lambda r, s: _plants_of("MD.Batch", r, s), "Plants holding the batch"),
    # --- accounting
    Relationship("FI.AccountingDocument", "FI.AccountingDocument", "DOC_FLOW", "AccountingDocument→Clearing", _clearing_docs, "Open item cleared by clearing/payment document"),
    Relationship("FI.AccountingDocument", "FI.AccountingDocument", "CROSS_COMPANY", "AccountingDocument→CrossCompanyCounterpart", _cross_company_docs, "Cross-company posting counterpart (BVORG)"),
    Relationship("FI.AccountingDocument", "MD.Customer", "MASTER_REF", "AccountingDocument→Customer", _bseg_partners("D", "KUNNR")),
    Relationship("FI.AccountingDocument", "MD.Vendor", "MASTER_REF", "AccountingDocument→Vendor", _bseg_partners("K", "LIFNR")),
    Relationship("FI.AccountingDocument", "FI.GLAccount", "MASTER_REF", "AccountingDocument→GLAccount", _bseg_field("HKONT")),
    Relationship("FI.AccountingDocument", "CO.CostCenter", "MASTER_REF", "AccountingDocument→CostCenter", _bseg_field("KOSTL", prefix_kokrs=True)),
    # --- master data ownership
    Relationship("MD.Customer", "CFG.CompanyCode", "ORG_OWNERSHIP", "Customer→CompanyCode", lambda r, s: sorted({x["BUKRS"] for x in s.lookup("KNB1", "KUNNR", r["KUNNR"])}), "Company-code view (KNB1); more than one owner = shared customer"),
    Relationship("MD.Vendor", "CFG.CompanyCode", "ORG_OWNERSHIP", "Vendor→CompanyCode", lambda r, s: sorted({x["BUKRS"] for x in s.lookup("LFB1", "LIFNR", r["LIFNR"])}), "Company-code view (LFB1); more than one owner = shared vendor"),
    Relationship("MD.Material", "CFG.Plant", "ORG_OWNERSHIP", "Material→Plant", lambda r, s: sorted({x["WERKS"] for x in s.lookup("MARC", "MATNR", r["MATNR"])}), "Plant view (MARC); plants in several company codes = shared material"),
    Relationship("MD.Material", "Z.ExportControl", "PARENT_CHILD", "Material→ExportControl", lambda r, s: [x["MATNR"] for x in s.lookup("ZSD_EXPORT_CTRL", "MATNR", r["MATNR"])], "Custom export-control classification"),
    Relationship("MD.Vendor", "Z.SupplierExt", "PARENT_CHILD", "Vendor→SupplierExtension", lambda r, s: [x["LIFNR"] for x in s.lookup("ZMM_SUPPLIER_EXT", "LIFNR", r["LIFNR"])]),
    Relationship("FI.FixedAsset", "CO.CostCenter", "MASTER_REF", "FixedAsset→CostCenter", lambda r, s: [f"{a['KOKRS']}|{r['KOSTL']}" for a in [s.get("TKA02", BUKRS=r["BUKRS"])] if a]),
    Relationship("CO.CostCenter", "CO.ProfitCenter", "MASTER_REF", "CostCenter→ProfitCenter", lambda r, s: [f"{r['KOKRS']}|{r['PRCTR']}"] if r.get("PRCTR") else []),
    Relationship("CFG.Plant", "CFG.CompanyCode", "ORG_OWNERSHIP", "Plant→CompanyCode", _plant_owner("WERKS")),
    Relationship("CFG.SalesOrg", "CFG.CompanyCode", "ORG_OWNERSHIP", "SalesOrg→CompanyCode", lambda r, s: [r["BUKRS"]]),
    Relationship("CFG.PurchOrg", "CFG.CompanyCode", "ORG_OWNERSHIP", "PurchOrg→CompanyCode", lambda r, s: [r["BUKRS"]]),
    Relationship("CFG.CompanyCode", "CFG.ControllingArea", "ORG_OWNERSHIP", "CompanyCode→ControllingArea", lambda r, s: [a["KOKRS"] for a in [s.get("TKA02", BUKRS=r["BUKRS"])] if a]),
]


def relationships_from(type_id: str) -> list[Relationship]:
    return [r for r in RELATIONSHIPS if r.from_type == type_id]


# --------------------------------------------------------------- S/4HANA compatibility registry
S4_COMPATIBILITY: list[dict] = [
    {"item": "Business Partner / CVI", "affects": ["MD.Customer", "MD.Vendor"], "ecc": "KNA1/LFA1 leading", "s4": "BUT000 leading, KNA1/LFA1 synchronised", "action": "Map customer/vendor to BP with roles FLCU00/FLVN00; number-range strategy required", "status": "MAPPED"},
    {"item": "Universal Journal", "affects": ["FI.AccountingDocument"], "ecc": "BKPF/BSEG + totals tables", "s4": "ACDOCA line items; BSEG retained for open items", "action": "Historical documents are posted through journal entry API; totals are derived", "status": "MAPPED"},
    {"item": "Material document", "affects": ["MM.MaterialDocument"], "ecc": "MKPF/MSEG", "s4": "MATDOC", "action": "Historical movements load as history; stock balances via migration object", "status": "MAPPED"},
    {"item": "New Asset Accounting", "affects": ["FI.FixedAsset"], "ecc": "ANLC values", "s4": "ACDOCA/FAAT_DOC_IT", "action": "Migrate master + balances via migration object; depreciation areas re-derived", "status": "MAPPED"},
    {"item": "Material Ledger", "affects": ["MD.Material"], "ecc": "Optional", "s4": "Mandatory (actual costing optional)", "action": "Activate ML in target; valuation migrated via product valuation object", "status": "MAPPED"},
    {"item": "Credit management", "affects": ["SD.SalesOrder", "MD.Customer"], "ecc": "FI-AR-CR (KNKK)", "s4": "FSCM credit management (UKMBP_CMS)", "action": "Credit master re-created; exposure recalculated", "status": "PLANNED"},
    {"item": "Profitability analysis", "affects": ["FI.AccountingDocument"], "ecc": "Costing-based CO-PA", "s4": "Account-based CO-PA in ACDOCA", "action": "Characteristic derivation mapping (planned)", "status": "PLANNED"},
    {"item": "Material number length", "affects": ["MD.Material"], "ecc": "18", "s4": "40 (extended optional)", "action": "No action unless extended MATNR activated", "status": "MAPPED"},
    {"item": "Custom fields / extensions", "affects": ["Z.ExportControl", "Z.TsaScope", "Z.SupplierExt"], "ecc": "Z tables", "s4": "Custom migration objects / CDS extensions", "action": "Each custom table requires a mapping and load decision", "status": "MAPPED"},
]


def load_methods_for(bo_type: BusinessObjectType, target_product: str) -> list[LoadMethod]:
    return [m for m in bo_type.load_methods if m.target == target_product]

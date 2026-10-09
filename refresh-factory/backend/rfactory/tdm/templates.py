"""Business-process scenario templates and candidate finders.

A template describes a *business scenario* (e.g. a complete order-to-cash chain) instead of tables and document numbers.
Candidate finders work on any read adapter, so the same logic discovers data in the source (to subset), in the target
(to catalog what already exists) and validates what the synthetic generator produces.
Only scenarios the current data model can express are `available`; the rest are listed as `planned` with the reason.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..sap.adapter import SourceAdapter


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    process: str
    description: str
    root_type: str
    include_downstream: tuple[str, ...]
    modes: tuple[str, ...]  # subset | synthetic
    coverage: str
    synthetic_stage: str | None = None
    status: str = "available"


TEMPLATES: dict[str, Template] = {t.id: t for t in [
    Template("o2c_complete", "Order-to-Cash: complete chain", "Order-to-Cash",
             "Sales order with delivery, billing document and the accounting document posted from it.",
             "SALES_ORDER", ("DELIVERY", "BILLING", "FI_DOCUMENT"), ("subset", "synthetic"),
             "Order, delivery, billing, FI posting. No credit management, pricing conditions, returns or payments.", "complete"),
    Template("o2c_delivered_unbilled", "Order-to-Cash: delivered, not yet billed", "Order-to-Cash",
             "Sales order with a delivery but no billing document: input for billing-run tests.",
             "SALES_ORDER", ("DELIVERY",), ("subset", "synthetic"), "Order and delivery only.", "delivered_unbilled"),
    Template("o2c_order_only", "Order-to-Cash: open order", "Order-to-Cash",
             "Sales order without any follow-on document: input for delivery-creation tests.",
             "SALES_ORDER", (), ("subset", "synthetic"), "Order header and items only.", "order_only"),
    Template("o2c_cross_company_plant", "Intercompany (partial): order shipping from another company's plant", "Intercompany",
             "Order of one company code with an item plant that belongs to a different company code.",
             "SALES_ORDER", ("DELIVERY",), ("subset",),
             "Covers only the cross-company plant reference. No intercompany billing or stock transfer.", None),
    Template("p2p_purchase_order", "Procure-to-Pay: purchase order", "Procure-to-Pay",
             "Purchase order with vendor (incl. bank details) and materials.",
             "PURCHASE_ORDER", (), ("subset", "synthetic"),
             "Up to the purchase order. Goods receipt, invoice receipt and payment are NOT modelled.", "po"),
    Template("r2r_billing_posting", "Record-to-Report: billing-originated FI posting", "Record-to-Report",
             "Balanced accounting document posted from a billing document (with its upstream chain).",
             "FI_DOCUMENT", (), ("subset",), "Revenue postings from SD only. No manual postings, clearing or open items.", None),
    Template("mfg_order_completed", "Make-to-Stock: completed production order", "Manufacturing",
             "Production order exploded from its BOM and routing, with confirmed operations, the component goods issues (261) and the finished-product goods receipt (101).",
             "PRODUCTION_ORDER", ("MATERIAL_DOCUMENT",), ("subset", "synthetic"),
             "Order, BOM, routing with operations, confirmations, reservations and goods movements with consistent quantities. No work-centre master, costing, batches or scrap.", "mfg_completed"),
    Template("mfg_order_open", "Make-to-Stock: open production order", "Manufacturing",
             "Production order with its BOM, routing and component reservations but no confirmation or goods movement yet: input for confirmation, goods-issue and goods-receipt tests.",
             "PRODUCTION_ORDER", (), ("subset", "synthetic"), "Order, BOM, routing and reservations only.", "mfg_open"),
    Template("md_bom_with_components", "Master data: BOM with component materials", "Manufacturing",
             "Bill of material with the product and every component material maintained in the plant.",
             "BOM", (), ("subset",), "BOM header and items with their materials. No alternative BOMs.", None),
    Template("md_customer_with_bank", "Master data: customer with bank details", "Master data",
             "Customer maintained for the company code, with address and bank details.",
             "CUSTOMER", (), ("subset", "synthetic"), "General, company code, sales area, bank, address.", "customer"),
    Template("md_materials", "Master data: materials in the company code's plants", "Master data",
             "Materials maintained in the plants of the company code: the prerequisite for synthetic documents in a clean target.",
             "MATERIAL", (), ("subset",), "General data, descriptions and plant data of the company code's plants.", None),
    Template("md_vendor_with_bank", "Master data: vendor with bank details", "Master data",
             "Vendor maintained for the company code, with address and bank details.",
             "VENDOR", (), ("subset", "synthetic"), "General, company code, bank, address.", "vendor"),
]}

PLANNED = [
    {"id": "make_to_order", "name": "Make-to-Order", "process": "Manufacturing", "status": "planned",
     "reason": "Needs sales-order-based planning and order-to-production links; routings/operations and confirmations are not modelled."},
    {"id": "asset_accounting", "name": "Asset Accounting", "process": "Record-to-Report", "status": "planned",
     "reason": "Asset master data and depreciation postings (FI-AA) are not modelled."},
    {"id": "inventory_wm", "name": "Inventory and warehouse management", "process": "Logistics", "status": "planned",
     "reason": "Stock, valuation and WM/EWM objects are not modelled (material documents for production orders are)."},
    {"id": "intercompany_billing", "name": "Intercompany processing (full)", "process": "Intercompany", "status": "planned",
     "reason": "Intercompany billing and stock transfers are not modelled; see the partial cross-company template."},
]


def describe(tpl: Template) -> dict:
    return {"id": tpl.id, "name": tpl.name, "process": tpl.process, "description": tpl.description, "root_type": tpl.root_type,
            "modes": list(tpl.modes), "coverage": tpl.coverage, "status": tpl.status,
            "includes": [tpl.root_type, *tpl.include_downstream]}


# ---------------------------------------------------------------- candidate finders
def _window(reader: SourceAdapter, params: dict):
    days = params.get("days")
    if not days:
        return "0000-00-00", "9999-12-31"
    ref = reader.reference_date()
    return (ref - timedelta(days=int(days))).isoformat(), ref.isoformat()


def _chain(r: SourceAdapter, order: str) -> dict:
    deliveries = sorted({l["VBELN"] for l in r.lookup("LIPS", "VGBEL", order)})
    billings = sorted({b["VBELN"] for d in deliveries for b in r.lookup("VBRP", "VGBEL", d)})
    fis = sorted(f"{h['BUKRS']}/{h['BELNR']}/{h['GJAHR']}" for b in billings for h in r.lookup("BKPF", "AWKEY", b)
                 if h["AWTYP"] == "VBRK")
    return {"deliveries": deliveries, "billings": billings, "fi": fis}


def find_candidates(tpl: Template, r: SourceAdapter, params: dict) -> list[dict]:
    """Roots (key + business attributes) in `r` that match the template. Deterministic order: newest first."""
    cc = params.get("company_code", "1000")
    lo, hi = _window(r, params)
    cust = params.get("customer")
    out: list[dict] = []
    if tpl.root_type == "SALES_ORDER":
        plant_cc = {p["WERKS"]: p["BUKRS"] for p in r.select("T001W")}
        for o in r.select("VBAK", lambda x: x["BUKRS_VF"] == cc and lo <= x["ERDAT"] <= hi and (not cust or x["KUNNR"] == cust)):
            ch = _chain(r, o["VBELN"])
            items = r.lookup("VBAP", "VBELN", o["VBELN"])
            if not items:
                continue
            ok = {"o2c_complete": ch["deliveries"] and ch["billings"] and ch["fi"],
                  "o2c_delivered_unbilled": ch["deliveries"] and not ch["billings"],
                  "o2c_order_only": not ch["deliveries"],
                  "o2c_cross_company_plant": any(plant_cc.get(i["WERKS"], cc) != cc for i in items)}[tpl.id]
            if ok:
                out.append({"key": o["VBELN"], "date": o["ERDAT"], "attrs": {
                    "company_code": cc, "customer": o["KUNNR"], "net_value": o["NETWR"], "currency": o["WAERK"], "items": len(items),
                    "deliveries": len(ch["deliveries"]), "billings": len(ch["billings"])}})
    elif tpl.root_type == "PURCHASE_ORDER":
        for o in r.select("EKKO", lambda x: x["BUKRS"] == cc and lo <= x["BEDAT"] <= hi and (not params.get("vendor") or x["LIFNR"] == params["vendor"])):
            items = r.lookup("EKPO", "EBELN", o["EBELN"])
            if items and r.get("LFA1", (o["LIFNR"],)) and all(r.get("MARA", (i["MATNR"],)) for i in items):
                out.append({"key": o["EBELN"], "date": o["BEDAT"], "attrs": {"company_code": cc, "vendor": o["LIFNR"], "items": len(items)}})
    elif tpl.root_type == "PRODUCTION_ORDER":
        item_t = "MATDOC" if "MATDOC" in {t for t in ("MATDOC",) if r.select("MATDOC")} else "MSEG"
        for o in r.select("AUFK", lambda x: x["BUKRS"] == cc and lo <= x["ERDAT"] <= hi):
            afpo, afko = r.lookup("AFPO", "AUFNR", o["AUFNR"]), r.get("AFKO", (o["AUFNR"],))
            if not afpo or not afko or not afko["STLNR"] or not r.get("STKO", (afko["STLNR"],)):
                continue
            moves = r.lookup(item_t, "AUFNR", o["AUFNR"])
            received = sum(a["WEMNG"] for a in afpo)
            ok = {"mfg_order_completed": received > 0 and any(m["BWART"] == "101" for m in moves) and any(m["BWART"] == "261" for m in moves),
                  "mfg_order_open": received == 0 and not moves}[tpl.id]
            if ok:
                out.append({"key": o["AUFNR"], "date": o["ERDAT"], "attrs": {
                    "company_code": cc, "plant": o["WERKS"], "product": afpo[0]["MATNR"], "quantity": afko["GAMNG"], "goods_movements": len({m["MBLNR"] for m in moves})}})
    elif tpl.root_type == "BOM":
        plants = {p["WERKS"] for p in r.select("T001W") if p["BUKRS"] == cc}
        for b in r.select("STKO", lambda x: x["WERKS"] in plants):
            comps = r.lookup("STPO", "STLNR", b["STLNR"])
            if comps and r.get("MARA", (b["MATNR"],)) and all(r.get("MARA", (c["IDNRK"],)) for c in comps):
                out.append({"key": b["STLNR"], "date": "", "attrs": {"company_code": cc, "plant": b["WERKS"], "product": b["MATNR"], "components": len(comps)}})
    elif tpl.root_type == "FI_DOCUMENT":
        for h in r.select("BKPF", lambda x: x["BUKRS"] == cc and x["AWTYP"] == "VBRK" and lo <= x["BUDAT"] <= hi):
            segs = [s for s in r.lookup("BSEG", "BELNR", h["BELNR"]) if s["BUKRS"] == h["BUKRS"] and s["GJAHR"] == h["GJAHR"]]
            deb = sum(s["DMBTR"] for s in segs if s["SHKZG"] == "S")
            cre = sum(s["DMBTR"] for s in segs if s["SHKZG"] == "H")
            if segs and abs(deb - cre) < 0.01 and r.get("VBRK", (h["AWKEY"],)):
                out.append({"key": f"{h['BUKRS']}/{h['BELNR']}/{h['GJAHR']}", "date": h["BUDAT"],
                            "attrs": {"company_code": cc, "billing": h["AWKEY"], "amount": deb, "currency": h["WAERS"]}})
    elif tpl.root_type == "MATERIAL":
        plants = {p["WERKS"] for p in r.select("T001W") if p["BUKRS"] == cc}
        for m in r.select("MARA"):
            if any(c["WERKS"] in plants for c in r.lookup("MARC", "MATNR", m["MATNR"])):
                out.append({"key": m["MATNR"], "date": "", "attrs": {"company_code": cc, "type": m["MTART"]}})
    elif tpl.root_type == "CUSTOMER":
        for k in r.select("KNA1"):
            if r.get("KNB1", (k["KUNNR"], cc)) and r.lookup("KNBK", "KUNNR", k["KUNNR"]) and (not cust or k["KUNNR"] == cust):
                out.append({"key": k["KUNNR"], "date": "", "attrs": {"company_code": cc, "country": k["LAND1"]}})
    elif tpl.root_type == "VENDOR":
        for v in r.select("LFA1"):
            if r.get("LFB1", (v["LIFNR"], cc)) and r.lookup("LFBK", "LIFNR", v["LIFNR"]):
                out.append({"key": v["LIFNR"], "date": "", "attrs": {"company_code": cc, "country": v["LAND1"]}})
    out.sort(key=lambda c: (c["date"], c["key"]), reverse=True)
    return out

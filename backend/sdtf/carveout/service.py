"""Carve-out intelligence: ParentCo / SpinCo separation reports derived from a manifest's classification
plus financial facts from the source record store (intercompany balances, open items, shared contracts)."""
from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.store import RecordStore
from ..models import ScopeManifest
from .deals import deal_assessment

DETECTIONS = [
    ("cross_company_postings", "Cross-company postings", "FI.AccountingDocument", lambda c: len(c["company_codes"]) > 1),
    ("shared_customers", "Shared customers", "MD.Customer", lambda c: len(c["company_codes"]) > 1),
    ("shared_vendors", "Shared vendors", "MD.Vendor", lambda c: len(c["company_codes"]) > 1),
    ("shared_materials", "Shared materials", "MD.Material", lambda c: len(c["company_codes"]) > 1),
    ("cross_company_sales", "Cross-company sales orders", "SD.SalesOrder", lambda c: len(c["company_codes"]) > 1),
    ("cross_company_purchasing", "Cross-company purchase orders", "MM.PurchaseOrder", lambda c: len(c["company_codes"]) > 1),
    ("cross_plant_transfers", "Cross-company stock transfers", "MM.MaterialDocument", lambda c: len(c["company_codes"]) > 1),
    ("open_sales_documents", "Open sales documents", "SD.SalesOrder", lambda c: c.get("status") == "OPEN"),
    ("open_purchase_documents", "Open purchase documents", "MM.PurchaseOrder", lambda c: c.get("status") == "OPEN"),
    ("open_accounting_items", "Open accounting items", "FI.AccountingDocument", lambda c: c.get("status") == "OPEN"),
    ("shared_cost_objects", "Shared controlling structures", "CFG.ControllingArea", lambda c: c["classification"] == "SHARED_DUPLICATED"),
    ("sensitive_records", "Export-controlled / sensitive records", "MD.Material", lambda c: "Export-controlled" in c["reason"]),
]


def carveout_classification(m: ScopeManifest) -> dict:
    cls = m.selection.get("classification", {})
    by_class: dict[str, list] = defaultdict(list)
    for nid, c in cls.items():
        by_class[c["classification"]].append({"node": nid, **{k: c[k] for k in ("type", "reason", "requires_approval", "company_codes", "status")}})
    return {"manifest_id": m.id, "counts": {k: len(v) for k, v in by_class.items()}, "buckets": {k: v[:200] for k, v in by_class.items()}}


def detections(m: ScopeManifest) -> list[dict]:
    cls = m.selection.get("classification", {})
    out = []
    for key, title, t, pred in DETECTIONS:
        hits = [nid for nid, c in cls.items() if c["type"] == t and pred(c)]
        out.append({"key": key, "title": title, "object_type": t, "count": len(hits), "samples": hits[:10]})
    return out


def intercompany_balances(store: RecordStore, scope_ccs: set[str]) -> list[dict]:
    """Open intercompany receivables/payables of in-scope company codes by counterpart (VBUND)."""
    bal: dict[tuple[str, str], dict] = defaultdict(lambda: {"receivable": 0.0, "payable": 0.0, "currency": ""})
    for l in store.rows("BSEG"):
        if l["BUKRS"] not in scope_ccs or not l.get("VBUND") or l.get("AUGBL"):
            continue
        if l["KOART"] not in ("D", "K"):
            continue
        cc = store.get("T001", BUKRS=l["BUKRS"])
        b = bal[(l["BUKRS"], l["VBUND"])]
        b["currency"] = cc["WAERS"] if cc else ""
        amt = l["DMBTR"] if l["SHKZG"] == "S" else -l["DMBTR"]
        if l["KOART"] == "D":
            b["receivable"] += amt
        else:
            b["payable"] += -amt
    return [{"company_code": k[0], "counterpart": k[1], "counterpart_in_scope": k[1] in scope_ccs, "receivable": round(v["receivable"], 2), "payable": round(v["payable"], 2), "currency": v["currency"]} for k, v in sorted(bal.items())]


def completeness_report(session: Session, m: ScopeManifest, store: RecordStore | None = None) -> dict:
    defn = m.definition
    scope_ccs = set(defn["company_codes"])
    store = store or RecordStore.load(session, defn["source_system_id"], tables=["BSEG", "T001", "KNB1", "LFB1", "MARC", "T001K", "VBAK", "EKKO", "BKPF"])
    cls = m.selection.get("classification", {})
    cc_counts: Counter = Counter()
    for c in cls.values():
        for cc in c["company_codes"]:
            cc_counts[cc] += 1
    # per-type coverage: objects owned by scope company codes vs classified as transferred
    owned: Counter = Counter()
    transferred: Counter = Counter()
    for nid, c in cls.items():
        if c["company_codes"] and c["company_codes"][0] in scope_ccs:
            owned[c["type"]] += 1
            if c["classification"] in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED"):
                transferred[c["type"]] += 1
    coverage = [{"type": t, "name": BUSINESS_OBJECTS[t].name if t in BUSINESS_OBJECTS else t, "owned": owned[t], "transferred": transferred[t], "coverage_pct": round(100.0 * transferred[t] / owned[t], 1) if owned[t] else 0.0} for t in sorted(owned)]
    pending = [{"node": nid, "type": c["type"], "classification": c["classification"], "reason": c["reason"]} for nid, c in cls.items() if c["requires_approval"]]
    return {
        "manifest_id": m.id,
        "manifest_version": m.version,
        "status": m.status,
        "scope_company_codes": sorted(scope_ccs),
        "coverage": coverage,
        "detections": detections(m),
        "intercompany_balances": intercompany_balances(store, scope_ccs),
        "pending_approvals": {"count": len(pending), "items": pending[:100]},
        "complete": len(pending) == 0 and all(c["coverage_pct"] >= 100.0 for c in coverage if BUSINESS_OBJECTS.get(c["type"], None) and BUSINESS_OBJECTS[c["type"]].kind != "TECHNICAL"),
    }


def cleanup_candidates(session: Session, m: ScopeManifest, store: RecordStore | None = None) -> list[dict]:
    """Every row of the source that still shows the carved-out company codes after the transfer: the company-code
    views of shared masters (customer, vendor, material) in scope; each with the action it would take, which is
    never executed without an approved cleanup plan."""
    defn = m.definition
    scope_ccs = set(defn["company_codes"])
    cls = m.selection.get("classification", {})
    store = store or RecordStore.load(session, defn["source_system_id"], tables=["KNB1", "LFB1", "MARC", "T001K"])
    cleanup = []
    for table, fld, t in (("KNB1", "KUNNR", "MD.Customer"), ("LFB1", "LIFNR", "MD.Vendor")):
        for r in store.rows(table):
            if r["BUKRS"] in scope_ccs:
                nid = f"{t}:{r[fld]}"
                c = cls.get(nid)
                if c and c["classification"] in ("SHARED_DUPLICATED", "FULLY_TRANSFERRED"):
                    cleanup.append({"table": table, "key": f"{r[fld]}|{r['BUKRS']}", "object": nid, "action": "DELETE_VIEW_AFTER_APPROVAL" if c["classification"] == "SHARED_DUPLICATED" else "ARCHIVE_AFTER_APPROVAL"})
    for r in store.rows("MARC"):
        k = store.get("T001K", BWKEY=r["WERKS"])
        if k and k["BUKRS"] in scope_ccs:
            cleanup.append({"table": "MARC", "key": f"{r['MATNR']}|{r['WERKS']}", "object": f"MD.Material:{r['MATNR']}", "action": "DELETE_VIEW_AFTER_APPROVAL"})
    return cleanup


def residual_exposure_report(session: Session, m: ScopeManifest, store: RecordStore | None = None) -> dict:
    """What stays behind in the source after the carve-out and what SpinCo data remains visible to ParentCo."""
    defn = m.definition
    scope_ccs = set(defn["company_codes"])
    cls = m.selection.get("classification", {})
    store = store or RecordStore.load(session, defn["source_system_id"], tables=["KNB1", "LFB1", "MARC", "T001K", "ZSD_EXPORT_CTRL", "ZFI_TSA_SCOPE"])
    # SpinCo data retained in the source by policy
    retained = [{"node": n, "type": c["type"], "reason": c["reason"]} for n, c in cls.items() if c["classification"] == "RETAINED_BY_SELLER" and c["company_codes"] and c["company_codes"][0] in scope_ccs]
    cleanup = cleanup_candidates(session, m, store)
    tsa = [r for r in store.rows("ZFI_TSA_SCOPE") if r["BUKRS"] in scope_ccs]
    excluded = Counter(c["type"] for c in cls.values() if c["classification"] == "EXCLUDED")
    deal = deal_assessment(defn)
    return {
        "manifest_id": m.id,
        "retained_spinco_history": {"count": len(retained), "samples": retained[:50]},
        "residual_cleanup_candidates": {"count": len(cleanup), "samples": cleanup[:50], "note": "Cleanup is never executed automatically; each action requires an approved residual cleanup plan (asset deal: the seller keeps its legal record, nothing is deleted)"},
        "excluded_by_policy": dict(excluded),
        "tsa_services": tsa,
        "export_control_flags": sum(1 for c in cls.values() if "Export-controlled" in c["reason"]),
        "deal": {"deal_type": deal.get("deal_type"), "residual_rule": deal.get("residual_rule"), "residual_note": deal.get("residual_note"), "obligations": deal.get("obligations", [])},
    }

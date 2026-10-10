"""Carve-out deal templates: what an asset deal, a share deal or a hive-down means for the scope policies, the
residual data in the seller's system and the approvals, and how a manifest deviates from its template.

Templates are defaults a business can override in the scope definition; they are not legal advice. The
consequences named here are the platform's own policies (which history moves, what stays behind, who signs),
not statements about any jurisdiction's law.
"""
from __future__ import annotations

DEAL_TEMPLATES: dict[str, dict] = {
    "ASSET_DEAL": {
        "name": "Asset deal",
        "summary": "The buyer acquires assets, contracts and open business; the legal entity (company code) stays with the seller. Open items, balances and the master data behind them move; the history stays in the seller's books as the legal record.",
        "legal_entity_transfers": False,
        "policies": {"historical_policy": "OPEN_ITEMS_AND_BALANCES", "document_status": "OPEN_ONLY", "shared_object_policy": "DUPLICATE", "cross_company_policy": "REFERENCE"},
        "residual_rule": "RETAIN_AS_LEGAL_RECORD",
        "residual_note": "The seller keeps every record of the transferred business as its legal record: the residual cleanup plan deactivates nothing and deletes nothing; views stay, flagged as transferred.",
        "obligations": [
            "The transfer agreement lists the assets, contracts and open items in scope; the manifest is checked against that list",
            "Open items settle through the transitional service agreement or are novated to the buyer",
            "History remains with the seller; the buyer receives balances and the open items at closing",
        ],
        "approvals": ["BUSINESS", "LEGAL"],
    },
    "SHARE_DEAL": {
        "name": "Share deal",
        "summary": "The legal entity transfers with its history: statutory retention follows the entity, so the full history moves and the seller must remove the entity's data from its system once the transitional service agreement ends.",
        "legal_entity_transfers": True,
        "policies": {"historical_policy": "FULL", "document_status": "ALL", "shared_object_policy": "DUPLICATE", "cross_company_policy": "INCLUDE_FLAG"},
        "residual_rule": "CLEANUP_AFTER_TSA",
        "residual_note": "The entity's data must leave the seller's system after the transitional service agreement: the residual cleanup plan removes the company-code views of shared masters and archives the transferred documents, each after approval.",
        "obligations": [
            "Full history transfers with the entity (statutory retention follows the entity)",
            "Intercompany balances are settled or documented before closing",
            "The seller removes the entity's data after the transitional service agreement (data protection, competition law)",
        ],
        "approvals": ["BUSINESS", "LEGAL", "FINANCE"],
    },
    "HIVE_DOWN": {
        "name": "Hive-down to a new entity",
        "summary": "The business is first moved into a new legal entity (a new company code in the target) and that entity is sold: a share deal with a company-code renumbering.",
        "legal_entity_transfers": True,
        "policies": {"historical_policy": "FULL", "document_status": "ALL", "shared_object_policy": "DUPLICATE", "cross_company_policy": "INCLUDE_FLAG"},
        "residual_rule": "CLEANUP_AFTER_TSA",
        "residual_note": "As for a share deal; the new company code must exist in the target before the load (configuration, number ranges, ledgers).",
        "obligations": [
            "The new company code is configured in the target before the initial load",
            "Every transferred object is renumbered to the new company code (target ownership map)",
            "The seller removes the entity's data after the transitional service agreement",
        ],
        "approvals": ["BUSINESS", "LEGAL", "FINANCE"],
        "requires_new_company_code": True,
    },
}

POLICY_CONSEQUENCE = {
    "historical_policy": "which history moves: FULL moves every document, OPEN_ITEMS_AND_BALANCES moves balances and open items only, YEARS moves the fiscal years from the cut",
    "document_status": "which documents move: ALL, OPEN_ONLY or CLOSED_ONLY",
    "shared_object_policy": "what happens to masters used by several company codes: DUPLICATE copies them, REFERENCE keeps them in the seller only, EXCLUDE drops them, MANUAL asks the business per object",
    "cross_company_policy": "what happens to documents spanning company codes: INCLUDE_FLAG moves them flagged, REFERENCE keeps a reference, EXCLUDE drops them",
}


def deal_templates() -> list[dict]:
    return [{"id": k, **v} for k, v in DEAL_TEMPLATES.items()]


def template(deal: str) -> dict:
    deal = (deal or "").upper()
    if deal not in DEAL_TEMPLATES:
        raise ValueError(f"unknown deal type {deal!r}; one of {', '.join(DEAL_TEMPLATES)}")
    return DEAL_TEMPLATES[deal]


def apply_deal(defn: dict, deal: str, new_company_code: str | None = None) -> dict:
    """A scope definition dictionary with the template's policies applied (explicit values in `defn` that differ
    are kept: the business decides, the assessment names the deviation). A hive-down maps the company codes to
    the new one."""
    t = template(deal)
    out = dict(defn)
    out["deal_type"] = (deal or "").upper()
    for k, v in t["policies"].items():
        out.setdefault(k, v)
    if t.get("requires_new_company_code"):
        if not new_company_code:
            raise ValueError("a hive-down needs the new company code of the target")
        own = dict(out.get("target_ownership") or {})
        own["company_code_map"] = {cc: new_company_code for cc in out.get("company_codes", [])}
        out["target_ownership"] = own
    desc = out.get("description") or ""
    tag = f"Deal type: {t['name']}."
    if tag not in desc:
        out["description"] = f"{desc} {tag}".strip()
    return out


def deal_assessment(defn: dict) -> dict:
    """How a manifest's definition relates to its deal template: the deviations with their consequence, the
    obligations and the residual rule. Without a deal type the assessment says so and names the templates."""
    deal = (defn.get("deal_type") or "").upper()
    if not deal:
        return {"deal_type": None, "template": None, "deviations": [], "obligations": [], "residual_rule": None, "note": "no deal type on the manifest: the scope policies were chosen by hand; choose a template to see what an asset deal, a share deal or a hive-down would change", "templates": list(DEAL_TEMPLATES)}
    t = template(deal)
    deviations = []
    for k, v in t["policies"].items():
        actual = defn.get(k)
        if actual != v:
            deviations.append({"field": k, "template": v, "actual": actual, "consequence": POLICY_CONSEQUENCE.get(k, "")})
    if t.get("requires_new_company_code"):
        cmap = (defn.get("target_ownership") or {}).get("company_code_map") or {}
        same = [cc for cc in defn.get("company_codes", []) if cmap.get(cc, cc) == cc]
        if same:
            deviations.append({"field": "target_ownership.company_code_map", "template": "a new company code for every transferred one", "actual": f"unchanged for {', '.join(same)}", "consequence": "a hive-down without renumbering loads the business under the seller's company code"})
    return {"deal_type": deal, "template": {"id": deal, "name": t["name"], "summary": t["summary"], "legal_entity_transfers": t["legal_entity_transfers"]}, "deviations": deviations, "obligations": t["obligations"], "approvals": t["approvals"], "residual_rule": t["residual_rule"], "residual_note": t["residual_note"], "note": "deviations are the business's choice; they are listed so the approver sees them, not refused"}


__all__ = ["DEAL_TEMPLATES", "deal_templates", "template", "apply_deal", "deal_assessment"]

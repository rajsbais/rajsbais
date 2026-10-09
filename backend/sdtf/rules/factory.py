"""Automated Transformation Rule Factory: derives a candidate ruleset from a scope definition and the
source/target metadata. Output is a draft that goes through validation, dry-run and approval."""
from __future__ import annotations

import yaml

from ..scope.models import ScopeDefinition


def generate_candidate_ruleset(defn: ScopeDefinition, target_product: str = "S4HANA", name: str | None = None, coa_map: dict[str, str] | None = None, source_index: int = 0, dedup: dict | None = None) -> str:
    """source_index selects disjoint number ranges and key prefixes per source system in a merger.
    dedup = {"customers": {src_key: survivor_target_key}, "vendors": {...}, "materials": {...}} maps duplicate master
    records of this source onto the survivor already loaded from another source; the duplicate master record itself is
    skipped, references are redirected."""
    cc_map = defn.target_ownership.company_code_map or {cc: cc for cc in defn.company_codes}
    plant_map = defn.target_ownership.plant_map
    kokrs_map = defn.target_ownership.controlling_area_map
    rules: list[dict] = [
        {"id": "cc-reassign", "type": "org_reassign", "description": "Company code reassignment ParentCo -> SpinCo", "tables": ["*"], "field": "BUKRS", "also_fields": ["BUKRS_VF", "VBUND"], "map": cc_map, "on_missing": "passthrough"},
        {"id": "sales-org-reassign", "type": "value_map", "description": "Sales/purchasing organisations follow the company code", "tables": ["TVKO", "T024E", "VBAK", "VBRK", "EKKO", "T001W"], "fields": ["VKORG", "EKORG"], "map": cc_map, "on_missing": "passthrough"},
    ]
    if plant_map:
        rules.append({"id": "plant-reassign", "type": "value_map", "description": "Plant / valuation area reassignment", "tables": ["*"], "fields": ["WERKS", "DWERK", "BWKEY", "UMWRK", "VSTEL"], "map": plant_map, "on_missing": "passthrough"})
    if kokrs_map:
        rules.append({"id": "kokrs-reassign", "type": "value_map", "description": "Controlling area reassignment", "tables": ["*"], "fields": ["KOKRS"], "map": kokrs_map, "on_missing": "passthrough"})
    if coa_map:
        rules.append({"id": "coa-harmonise", "type": "value_map", "description": "Chart-of-accounts harmonisation", "tables": ["BSEG", "SKB1", "SKA1", "KNB1", "LFB1"], "fields": ["HKONT", "SAKNR", "AKONT"], "lookup": "coa_map", "on_missing": "error"})
    lookups: dict = {"coa_map": coa_map} if coa_map else {}
    prefix = "BP" if source_index == 0 else f"BP{source_index + 1}"
    dedup = dedup or {}
    if dedup:
        # redirect references of duplicate masters to the survivor's target key, skip the duplicate master records
        for kind, tables, fields, hdr in (("customers", ["KNA1", "KNB1", "VBAK", "VBRK", "LIKP", "BSEG", "BSID"], ["KUNNR", "KUNRG"], ["KNA1"]), ("vendors", ["LFA1", "LFB1", "EKKO", "RBKP", "BSEG", "BSIK", "ZMM_SUPPLIER_EXT"], ["LIFNR"], ["LFA1", "ZMM_SUPPLIER_EXT"]), ("materials", ["MARA", "MARC", "MBEW", "MARD", "VBAP", "LIPS", "VBRP", "EKPO", "MSEG", "RSEG", "AFKO", "AFPO", "ZSD_EXPORT_CTRL"], ["MATNR", "PLNBEZ"], ["MARA", "ZSD_EXPORT_CTRL"])):
            m = dedup.get(kind) or {}
            if not m:
                continue
            lookups[f"dedup_{kind}"] = m
            rules.append({"id": f"dedup-skip-{kind}", "type": "skip", "description": f"Duplicate {kind} already loaded from another source are not re-created (general data and client-level extensions)", "tables": hdr, "when": {"field": fields[0], "in_lookup": f"dedup_{kind}"}, "message": f"duplicate {kind[:-1]} merged into survivor"})
            rule = {"id": f"dedup-{kind}", "type": "key_map", "description": f"Redirect {kind} references to the surviving master record", "tables": tables, "fields": fields, "strategy": "lookup", "lookup": f"dedup_{kind}", "on_missing": "passthrough"}
            if kind != "materials":
                rule["emit_field"] = "PARTNER"  # views of redirected partners carry the same BP reference as the survivor's
            rules.append(rule)
    if source_index > 0:
        # merger: every document / asset number of a non-leading source moves into a disjoint range so that
        # references (document flow, PO history, clearing, settlement) stay consistent across tables
        rules.append({"id": "merge-document-numbers", "type": "number_range", "description": f"Disjoint document number range for source #{source_index + 1}", "tables": ["VBAK", "VBAP", "VBFA", "LIKP", "LIPS", "VBRK", "VBRP", "EKKO", "EKPO", "EKBE", "MKPF", "MSEG", "RBKP", "RSEG", "BKPF", "BSEG", "BSID", "BSIK", "AFKO", "AFPO", "AUFK", "AFRU", "ANLA", "ANLC"], "fields": ["VBELN", "VGBEL", "AUBEL", "VBELV", "EBELN", "MBLNR", "BELNR", "AUGBL", "AUFNR", "RUECK", "ANLN1"], "offset": 10_000_000_000 * source_index})
    if target_product == "S4HANA":
        bp_when = {"field": "KUNNR", "not_prefix": "BP"}
        rules += [
            {"id": "bp-customer", "type": "key_map", "description": "Customer -> Business Partner number (CVI)", "tables": ["KNA1", "KNB1", "VBAK", "LIKP", "BSEG", "BSID"], "fields": ["KUNNR"], "strategy": "prefix", "prefix": prefix, "emit_field": "PARTNER", "when": bp_when},
            {"id": "bp-payer", "type": "key_map", "description": "Payer -> Business Partner number (CVI)", "tables": ["VBRK"], "fields": ["KUNRG"], "strategy": "prefix", "prefix": prefix, "when": {"field": "KUNRG", "not_prefix": "BP"}},
            {"id": "bp-vendor", "type": "key_map", "description": "Vendor -> Business Partner number (CVI)", "tables": ["LFA1", "LFB1", "EKKO", "RBKP", "BSEG", "BSIK", "ZMM_SUPPLIER_EXT"], "fields": ["LIFNR"], "strategy": "prefix", "prefix": prefix, "emit_field": "PARTNER", "when": {"field": "LIFNR", "not_prefix": "BP"}},
            {"id": "fi-number-range", "type": "number_range", "description": "Accounting document number range offset to avoid collisions in the target", "tables": ["BKPF", "BSEG", "BSID", "BSIK"], "fields": ["BELNR", "AUGBL"], "offset": 500000000 + source_index * 100000000},
            {"id": "ledger-default", "type": "default", "description": "Universal Journal leading ledger", "tables": ["BKPF"], "set": {"RLDNR": "0L"}},
            {"id": "reject-stat-docs", "type": "reject", "description": "Statistical / noted items are not migrated", "tables": ["BKPF"], "when": {"field": "BSTAT", "in": ["S", "V"]}, "message": "statistical or parked document not migrated"},
        ]
    doc = {
        "ruleset": name or f"{defn.name}-rules",
        "version": 1,
        "description": f"Candidate rules generated for scope '{defn.name}' ({defn.scenario_type})",
        "applies_to": {"source": {"product": "ECC"}, "target": {"product": target_product}},
        "lookups": lookups,
        "rules": rules,
        "tests": [
            {"name": "company code reassigned", "table": "BKPF", "input": {"BUKRS": defn.company_codes[0], "BELNR": "100000001", "GJAHR": 2024, "BSTAT": ""}, "expected": {"BUKRS": cc_map.get(defn.company_codes[0], defn.company_codes[0])}},
        ],
    }
    if target_product == "S4HANA":
        doc["tests"].append({"name": "parked document rejected", "table": "BKPF", "input": {"BUKRS": defn.company_codes[0], "BELNR": "1", "GJAHR": 2024, "BSTAT": "V"}, "expect_reject": True})
        if not (dedup.get("customers") or {}).get("100001"):
            doc["tests"].append({"name": "customer becomes business partner", "table": "KNA1", "input": {"KUNNR": "100001", "NAME1": "X"}, "expected": {"KUNNR": f"{prefix}100001", "PARTNER": f"{prefix}100001"}})
    return yaml.safe_dump(doc, sort_keys=False)

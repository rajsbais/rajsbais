"""Automated Transformation Rule Factory: derives a candidate ruleset from a scope definition and the
source/target metadata. Output is a draft that goes through validation, dry-run and approval."""
from __future__ import annotations

import yaml

from ..scope.models import ScopeDefinition


def generate_candidate_ruleset(defn: ScopeDefinition, target_product: str = "S4HANA", name: str | None = None, coa_map: dict[str, str] | None = None) -> str:
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
    if target_product == "S4HANA":
        rules += [
            {"id": "bp-customer", "type": "key_map", "description": "Customer -> Business Partner number (CVI)", "tables": ["KNA1", "KNB1", "VBAK", "VBRK", "LIKP", "BSEG", "BSID"], "fields": ["KUNNR", "KUNRG"], "strategy": "prefix", "prefix": "BP", "emit_field": "PARTNER"},
            {"id": "bp-vendor", "type": "key_map", "description": "Vendor -> Business Partner number (CVI)", "tables": ["LFA1", "LFB1", "EKKO", "RBKP", "BSEG", "BSIK", "ZMM_SUPPLIER_EXT"], "fields": ["LIFNR"], "strategy": "prefix", "prefix": "BP", "emit_field": "PARTNER"},
            {"id": "fi-number-range", "type": "number_range", "description": "Accounting document number range offset to avoid collisions in the target", "tables": ["BKPF", "BSEG", "BSID", "BSIK"], "fields": ["BELNR", "AUGBL"], "offset": 500000000},
            {"id": "ledger-default", "type": "default", "description": "Universal Journal leading ledger", "tables": ["BKPF"], "set": {"RLDNR": "0L"}},
            {"id": "reject-stat-docs", "type": "reject", "description": "Statistical / noted items are not migrated", "tables": ["BKPF"], "when": {"field": "BSTAT", "in": ["S", "V"]}, "message": "statistical or parked document not migrated"},
        ]
    doc = {
        "ruleset": name or f"{defn.name}-rules",
        "version": 1,
        "description": f"Candidate rules generated for scope '{defn.name}' ({defn.scenario_type})",
        "applies_to": {"source": {"product": "ECC"}, "target": {"product": target_product}},
        "lookups": {"coa_map": coa_map} if coa_map else {},
        "rules": rules,
        "tests": [
            {"name": "company code reassigned", "table": "BKPF", "input": {"BUKRS": defn.company_codes[0], "BELNR": "100000001", "GJAHR": 2024, "BSTAT": ""}, "expected": {"BUKRS": cc_map.get(defn.company_codes[0], defn.company_codes[0])}},
        ],
    }
    if target_product == "S4HANA":
        doc["tests"].append({"name": "parked document rejected", "table": "BKPF", "input": {"BUKRS": defn.company_codes[0], "BELNR": "1", "GJAHR": 2024, "BSTAT": "V"}, "expect_reject": True})
        doc["tests"].append({"name": "customer becomes business partner", "table": "KNA1", "input": {"KUNNR": "100001", "NAME1": "X"}, "expected": {"KUNNR": "BP100001", "PARTNER": "BP100001"}})
    return yaml.safe_dump(doc, sort_keys=False)

import pytest

from sdtf.rules.engine import RuleError, dry_run, parse_ruleset, run_tests, transform_record, validate_ruleset
from sdtf.rules.factory import generate_candidate_ruleset
from sdtf.scope.models import ScopeDefinition, TargetOwnership

YAML = """
ruleset: unit
version: 1
lookups:
  coa: {"140000": "12100000", "800000": "41000000"}
rules:
  - id: cc
    type: org_reassign
    field: BUKRS
    map: {"5000": "SP01"}
  - id: coa
    type: value_map
    tables: [BSEG]
    fields: [HKONT]
    lookup: coa
    on_missing: error
  - id: bp
    type: key_map
    tables: [KNA1, BSEG]
    fields: [KUNNR]
    strategy: prefix
    prefix: BP
  - id: nr
    type: number_range
    tables: [BKPF, BSEG]
    fields: [BELNR]
    offset: 1000
  - id: fx
    type: currency_convert
    tables: [BSEG]
    fields: [DMBTR]
    currency_field: WAERS
    to: EUR
    rates: {"USD->EUR": 0.5}
  - id: dflt
    type: default
    tables: [BKPF]
    set: {RLDNR: "0L"}
  - id: cond
    type: conditional
    tables: [BKPF]
    when: {field: BLART, equals: RV}
    then: {set: {XREF1: MIG}}
  - id: rej
    type: reject
    tables: [BKPF]
    when: {field: BSTAT, in: [V]}
    message: parked
tests:
  - name: cc map
    table: BKPF
    input: {BUKRS: "5000", BELNR: "1", GJAHR: 2024, BLART: RV, BSTAT: ""}
    expected: {BUKRS: SP01, BELNR: "1001", RLDNR: "0L", XREF1: MIG}
  - name: reject parked
    table: BKPF
    input: {BUKRS: "5000", BELNR: "1", GJAHR: 2024, BSTAT: V}
    expect_reject: true
"""


def test_parse_validate_and_embedded_tests():
    rs = parse_ruleset(YAML)
    v = validate_ruleset(rs)
    assert v["ok"], v
    assert v["tests"]["passed"] == 2 and v["tests"]["failed"] == 0
    assert any("not idempotent" in w for w in v["warnings"])


def test_transform_is_deterministic_with_lineage():
    rs = parse_ruleset(YAML)
    rec = {"BUKRS": "5000", "BELNR": "7", "GJAHR": 2024, "BUZEI": 1, "HKONT": "140000", "KUNNR": "100001", "DMBTR": 10.0, "WAERS": "USD"}
    a, la = transform_record(rs, "BSEG", rec)
    b, lb = transform_record(rs, "BSEG", rec)
    assert a == b and la == lb
    assert a["BUKRS"] == "SP01" and a["HKONT"] == "12100000" and a["KUNNR"] == "BP100001" and a["BELNR"] == "1007"
    assert a["DMBTR"] == 5.0 and a["WAERS"] == "EUR"
    assert {l["rule"] for l in la} == {"cc", "coa", "bp", "nr", "fx"}
    assert rec["BUKRS"] == "5000", "input must not be mutated"


def test_missing_mapping_raises():
    rs = parse_ruleset(YAML)
    with pytest.raises(RuleError) as e:
        transform_record(rs, "BSEG", {"BUKRS": "5000", "BELNR": "7", "GJAHR": 2024, "BUZEI": 1, "HKONT": "999999"})
    assert e.value.rule_id == "coa"


def test_validation_catches_errors():
    bad = """
ruleset: bad
rules:
  - id: a
    type: value_map
    tables: [NOPE]
    fields: [X]
    map: {"1": "2"}
  - id: a
    type: unknown
  - id: b
    type: value_map
    tables: [BKPF]
    fields: [NOTAFIELD]
    lookup: missing
"""
    v = validate_ruleset(parse_ruleset(bad))
    assert not v["ok"]
    joined = " ".join(v["errors"])
    assert "unknown table" in joined and "duplicate rule id" in joined and "unknown rule type" in joined and "does not exist" in joined and "not defined" in joined


def test_dry_run_reports_impact():
    rs = parse_ruleset(YAML)
    out = dry_run(rs, {"BKPF": [{"BUKRS": "5000", "BELNR": "1", "GJAHR": 2024, "BLART": "SA", "BSTAT": ""}, {"BUKRS": "1000", "BELNR": "2", "GJAHR": 2024, "BLART": "SA", "BSTAT": "V"}]})
    assert out["records"] == 2 and out["changed"] == 1 and len(out["exceptions"]) == 1
    assert out["rule_impact"]["cc"] == 1


def test_rule_factory_generates_valid_ruleset():
    defn = ScopeDefinition(name="x", source_system_id="s", target_system_id="t", company_codes=["5000"], target_ownership=TargetOwnership(company_code_map={"5000": "SP01"}, plant_map={"5010": "SP10"}))
    y = generate_candidate_ruleset(defn, "S4HANA", coa_map={"140000": "12100000"})
    rs = parse_ruleset(y)
    v = validate_ruleset(rs)
    assert v["ok"], v
    assert run_tests(rs)["failed"] == 0
    ids = {r["id"] for r in rs.rules}
    assert {"cc-reassign", "plant-reassign", "coa-harmonise", "bp-customer", "fi-number-range"} <= ids

"""Rule editor: the structured document composed into canonical YAML and saved as a version, lookup tables from
CSV exports, the grid with its decisions, per-rule approval under four eyes and the set's approval guarded by it,
decisions carried into the next version for unchanged rules; the CLI conversion."""
import json

import pytest

from sdtf.rules.editor import _mapping_text, compose_ruleset, normalise_rule, parse_lookup_csv
from sdtf.rules.engine import parse_ruleset, validate_ruleset

API = "/api/v1"
DOC = {
    "ruleset": "editor-unit",
    "description": "from the grid",
    "applies_to": {"source": {"product": "ECC"}, "target": {"product": "S4HANA"}},
    "lookups": {"coa": {"140000": "12100000"}},
    "rules": [
        {"id": "cc", "type": "org_reassign", "tables": "*", "field": "BUKRS", "map": {"5000": "SP01"}, "on_missing": "passthrough", "description": ""},
        {"id": "coa", "type": "value_map", "tables": "BSEG, SKB1", "fields": "HKONT, SAKNR", "lookup": "coa", "on_missing": "error"},
        {"id": "parked", "type": "reject", "tables": ["BKPF"], "when": {"field": "BSTAT", "in": ["S", "V"]}, "message": "parked"},
    ],
    "tests": [{"name": "cc", "table": "BKPF", "input": {"BUKRS": "5000", "BELNR": "1", "GJAHR": 2024, "BSTAT": ""}, "expected": {"BUKRS": "SP01"}}],
}


def test_compose_round_trip_and_normalisation():
    assert normalise_rule({"id": "x", "type": "default", "tables": "BKPF,BSEG", "set": {"RLDNR": "0L"}, "description": "", "map": {}}) == {"id": "x", "type": "default", "tables": ["BKPF", "BSEG"], "set": {"RLDNR": "0L"}}
    src = compose_ruleset(DOC)
    rs = parse_ruleset(src)
    assert rs.name == "editor-unit" and rs.version == 1 and rs.description == "from the grid" and rs.lookups == {"coa": {"140000": "12100000"}}
    assert [r["id"] for r in rs.rules] == ["cc", "coa", "parked"] and rs.rules[1]["tables"] == ["BSEG", "SKB1"] and rs.rules[1]["fields"] == ["HKONT", "SAKNR"]
    assert "description" not in rs.rules[0]
    v = validate_ruleset(rs)
    assert v["ok"], v
    assert compose_ruleset(DOC) == src  # canonical: same document, same YAML, same hash
    with pytest.raises(ValueError):
        compose_ruleset({"rules": []})
    assert _mapping_text({"type": "lookup_enrich", "key_field": "KOSTL", "lookup": "cc_to_pc", "set_field": "PRCTR"}, {}) == "PRCTR from cc_to_pc by KOSTL"
    assert _mapping_text({"type": "value_map", "fields": ["X"], "map": {"a": 1, "b": 2, "c": 3, "d": 4}}, {}) == "a → 1, b → 2, c → 3 (+1)"
    assert _mapping_text({"type": "key_map", "fields": ["KUNNR"], "strategy": "prefix", "prefix": "BP"}, {}) == "prefix BP"


def test_lookup_csv_parsing():
    rep = parse_lookup_csv("﻿source,target\n140000,12100000\n800000,41000000\n140000,12100000\n", "coa_map")
    assert rep["header"] and rep["delimiter"] == "," and rep["entries"] == {"140000": "12100000", "800000": "41000000"}
    assert rep["rows"] == 3 and rep["duplicates"] == [{"row": 4, "key": "140000"}] and not rep["errors"]
    rep = parse_lookup_csv("140000;12100000\n800000;41000000\n\n;x\n", "coa_map")
    assert not rep["header"] and rep["delimiter"] == ";" and rep["entries"] == {"140000": "12100000", "800000": "41000000"} and rep["skipped"] == 1
    rep = parse_lookup_csv("ECC account\tS4 account\tnote\n1\t10\ta\n1\t11\tb\n2\n", "coa", key_column="ECC account", value_column="S4 account")
    assert rep["columns"] == ["ECC account", "S4 account", "note"] and rep["entries"] == {"1": "10"} and rep["conflicts"] == [{"row": 3, "key": "1", "first": "10", "other": "11"}]
    assert any("different values" in e for e in rep["errors"]) and any("row 4" in e for e in rep["errors"])
    assert parse_lookup_csv("a,b\n1,2\n", "coa", key_column="nope")["errors"][0].startswith("columns")
    assert parse_lookup_csv("", "coa")["errors"] == ["the file holds no rows"]
    assert "alphanumeric" in parse_lookup_csv("1,2\n", "bad name")["errors"][0]


def test_grid_decisions_and_approval(client, tokens, slice_result):
    pid = slice_result["project_id"]
    # the editor saves a document; a plain YAML or a document is required
    assert client.post(f"{API}/projects/{pid}/rulesets", json={}, headers=tokens["architect"]).status_code == 400
    assert client.post(f"{API}/projects/{pid}/rulesets", json={"document": {"rules": []}}, headers=tokens["architect"]).status_code == 400
    comp = client.post(f"{API}/projects/{pid}/rulesets/compose", json={"document": DOC}, headers=tokens["viewer"]).json()
    assert comp["validation"]["ok"] and comp["source_yaml"].startswith("ruleset: editor-unit") and comp["document"]["rules"][1]["tables"] == ["BSEG", "SKB1"]
    back = client.post(f"{API}/projects/{pid}/rulesets/compose", json={"source_yaml": comp["source_yaml"]}, headers=tokens["viewer"]).json()
    assert back["document"] == comp["document"] and back["source_yaml"] == comp["source_yaml"]
    assert not client.post(f"{API}/projects/{pid}/rulesets/compose", json={"document": {"ruleset": "x", "rules": [{"id": "a", "type": "nope"}]}}, headers=tokens["viewer"]).json()["validation"]["ok"]
    assert client.post(f"{API}/projects/{pid}/rulesets/compose", json={}, headers=tokens["viewer"]).json()["validation"]["errors"] == ["give document or source_yaml"]
    r = client.post(f"{API}/projects/{pid}/rulesets", json={"document": DOC}, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    rs = r.json()
    assert rs["source_yaml"] == comp["source_yaml"] and rs["decisions"] == {"approved": 0, "rejected": 0, "pending": 3, "rules": 3}
    # the grid
    g = client.get(f"{API}/rulesets/{rs['id']}/rules", headers=tokens["viewer"]).json()
    rows = {x["id"]: x for x in g["rules"]}
    assert [x["position"] for x in g["rules"]] == [1, 2, 3] and g["lookups"] == {"coa": 1} and g["document"]["ruleset"] == "editor-unit"
    assert rows["cc"]["fields"] == ["BUKRS"] and rows["cc"]["mapping"] == "5000 → SP01" and rows["cc"]["decision"] == "PENDING"
    assert rows["coa"]["mapping"] == "lookup coa (1 entries)" and rows["coa"]["tables"] == ["BSEG", "SKB1"] and rows["coa"]["on_missing"] == "error"
    assert rows["parked"]["condition"] == 'BSTAT in ["S", "V"]' and rows["parked"]["mapping"] == "parked"
    # decisions: approver only, four eyes, rejection needs a comment, unknown rule
    dec = lambda who, rule, body: client.post(f"{API}/rulesets/{rs['id']}/rules/{rule}/decision", json=body, headers=tokens[who])  # noqa: E731
    assert dec("architect", "cc", {"decision": "APPROVED"}).status_code == 403
    assert dec("approver", "nope", {"decision": "APPROVED"}).status_code == 404
    assert dec("approver", "coa", {"decision": "REJECTED"}).status_code == 400
    assert dec("approver", "coa", {"decision": "MAYBE"}).status_code == 422
    d = dec("approver", "coa", {"decision": "REJECTED", "comment": "chart of accounts not signed off"}).json()
    assert d["decision"] == "REJECTED" and d["by"] == "approver" and d["decisions"] == {"approved": 0, "rejected": 1, "pending": 2, "rules": 3}
    assert dec("approver", "cc", {"decision": "APPROVED", "comment": "ok"}).json()["decisions"]["approved"] == 1
    # the set cannot be approved while a rule is rejected
    a = client.post(f"{API}/rulesets/{rs['id']}/approve", json={}, headers=tokens["approver"])
    assert a.status_code == 400 and "coa" in a.json()["detail"]
    # a new version without the rejected rule, based on this one: cc keeps its approval, a changed rule starts pending
    doc2 = {**DOC, "rules": [DOC["rules"][0], {**DOC["rules"][2], "message": "parked or statistical"}]}
    r2 = client.post(f"{API}/projects/{pid}/rulesets", json={"document": doc2, "based_on": rs["id"]}, headers=tokens["architect"])
    assert r2.status_code == 201, r2.text
    rs2 = r2.json()
    assert rs2["version"] == 2 and rs2["decisions"] == {"approved": 1, "rejected": 0, "pending": 1, "rules": 2}
    g2 = {x["id"]: x for x in client.get(f"{API}/rulesets/{rs2['id']}/rules", headers=tokens["viewer"]).json()["rules"]}
    assert g2["cc"]["decision"] == "APPROVED" and g2["cc"]["carried_from"] == 1 and g2["parked"]["decision"] == "PENDING"
    assert client.post(f"{API}/projects/{pid}/rulesets", json={"document": doc2, "based_on": "nope"}, headers=tokens["architect"]).status_code == 404
    # approval of the set approves the pending rule with it, recorded per rule; decisions are frozen afterwards
    a = client.post(f"{API}/rulesets/{rs2['id']}/approve", json={"comment": "release"}, headers=tokens["approver"])
    assert a.status_code == 200, a.text
    assert a.json()["decisions"] == {"approved": 2, "rejected": 0, "pending": 0, "rules": 2}
    g3 = {x["id"]: x for x in client.get(f"{API}/rulesets/{rs2['id']}/rules", headers=tokens["viewer"]).json()["rules"]}
    assert g3["parked"]["decided_by"] == "approver" and g3["parked"]["comment"] == "release" and g3["cc"]["comment"] == "ok"
    frozen = client.post(f"{API}/rulesets/{rs2['id']}/rules/cc/decision", json={"decision": "PENDING"}, headers=tokens["approver"])
    assert frozen.status_code == 400 and "frozen" in frozen.json()["detail"]
    assert dec("approver", "cc", {"decision": "PENDING"}).json()["decision"] == "PENDING"  # version 1 is still a draft
    ev = client.get(f"{API}/audit/events", params={"subject_id": rs["id"]}, headers=tokens["auditor"]).json()
    kinds = [e["action"] for e in ev]
    assert "RULE_DECIDED" in kinds
    # lookup from CSV through the API
    lk = client.post(f"{API}/rulesets/lookups/from-csv", json={"name": "coa_map", "text": "source;target\n140000;12100000\n"}, headers=tokens["viewer"]).json()
    assert lk["entries"] == {"140000": "12100000"} and lk["yaml"] == "lookups:\n  coa_map:\n    '140000': '12100000'\n"
    assert client.post(f"{API}/rulesets/lookups/from-csv", json={"name": "coa_map", "text": "1,2\n1,3\n"}, headers=tokens["viewer"]).json()["yaml"] is None


def test_lookup_csv_cli(tmp_path, capsys):
    from sdtf.cli import main

    f = tmp_path / "coa.csv"
    f.write_text("from,to\n140000,12100000\n800000,41000000\n", encoding="utf-8")
    assert main(["lookup-csv", "--file", str(f), "--name", "coa_map"]) == 0
    out = capsys.readouterr()
    assert out.out == "lookups:\n  coa_map:\n    '140000': '12100000'\n    '800000': '41000000'\n" and "2 entries from 2 rows" in out.err
    assert main(["lookup-csv", "--file", str(f), "--name", "coa_map", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entries"]["800000"] == "41000000"
    f.write_text("1,2\n1,3\n", encoding="utf-8")
    assert main(["lookup-csv", "--file", str(f), "--name", "coa_map"]) == 2
    assert "different values" in capsys.readouterr().err

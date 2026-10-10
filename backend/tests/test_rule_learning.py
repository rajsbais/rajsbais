"""Rule factory v2: mappings learned from approved rule sets of other projects, with provenance and conflicts,
proposed as learned rules in a candidate rule set; API and CLI."""
import hashlib

import pytest

from sdtf.models import Project, RuleSet
from sdtf.rules.engine import parse_ruleset, validate_ruleset
from sdtf.rules.learning import learn_mappings, learned_rules

API = "/api/v1"


def _approved(session, project, name, rules, lookups, approver="approver"):
    doc = f"ruleset: {name}\nversion: 1\nrules: []\n"
    rs = RuleSet(project_id=project.id, name=name, version=1, content_hash=hashlib.sha256(doc.encode()).hexdigest(), source_yaml=doc, compiled={"rules": rules, "lookups": lookups, "applies_to": {}}, validation={"ok": True}, status="APPROVED", created_by="architect", approved_by=approver)
    session.add(rs)
    session.flush()
    return rs


@pytest.fixture()
def other_projects(session):
    a = Project(name="Project Borealis - divestment", tenant_id="default", scenario_type="CARVE_OUT", created_by="architect")
    b = Project(name="Project Cirrus - merger", tenant_id="default", scenario_type="MERGER", created_by="architect")
    session.add_all([a, b])
    session.flush()
    ra = _approved(session, a, "borealis-rules", [
        {"id": "cc", "type": "org_reassign", "tables": ["*"], "field": "BUKRS", "map": {"3000": "SP03"}},
        {"id": "coa", "type": "value_map", "tables": ["BSEG", "SKB1"], "fields": ["HKONT", "SAKNR"], "lookup": "coa", "on_missing": "error"},
        {"id": "pay", "type": "value_map", "tables": ["KNB1"], "fields": ["ZTERM"], "map": {"Z001": "NT30", "Z002": "NT60"}},
    ], {"coa": {"140000": "12100000", "160000": "21100000", "800000": "41000000"}})
    rb = _approved(session, b, "cirrus-rules", [
        {"id": "coa2", "type": "value_map", "tables": ["BSEG"], "fields": ["HKONT"], "map": {"140000": "12100000", "800000": "41000099"}},
        {"id": "draft-only", "type": "value_map", "tables": ["KNB1"], "fields": ["ZTERM"], "map": {"Z009": "NT90"}},
    ], {}, approver="approver2")
    draft = RuleSet(project_id=b.id, name="cirrus-draft", version=1, content_hash="x" * 64, source_yaml="ruleset: cirrus-draft\n", compiled={"rules": [{"id": "never", "type": "value_map", "tables": ["BSEG"], "fields": ["HKONT"], "map": {"999999": "1"}}], "lookups": {}}, validation={"ok": True}, status="DRAFT", created_by="architect")
    session.add(draft)
    session.commit()  # the API runs in its own session
    yield {"a": a, "b": b, "ra": ra, "rb": rb}
    for x in (draft, ra, rb):
        session.delete(x)
    session.flush()
    for x in (a, b):
        session.delete(x)
    session.commit()


def test_learn_mappings_with_provenance_and_conflicts(session, slice_result, other_projects):
    L = learn_mappings(session, "default", exclude_project=slice_result["project_id"])
    assert L["approved_rulesets"] >= 2 and "BUKRS" not in L["fields"]  # organisational maps are not learned by default
    h = L["fields"]["HKONT"]
    assert h["entries"] == {"140000": "12100000", "160000": "21100000"} and h["support"]["140000"] == 2 and h["support"]["160000"] == 1
    assert set(h["conflicts"]) == {"800000"} and set(h["conflicts"]["800000"]) == {"41000000", "41000099"} and h["conflicts"]["800000"]["41000099"][0]["project"] == "Project Cirrus - merger"
    assert {s["ruleset"] for s in h["sources"]} == {"borealis-rules", "cirrus-rules"} and "BSEG" in h["tables"] and "999999" not in h["entries"]  # the draft set teaches nothing
    assert L["fields"]["SAKNR"]["entries"]["140000"] == "12100000" and L["fields"]["ZTERM"]["entries"] == {"Z001": "NT30", "Z002": "NT60", "Z009": "NT90"}
    assert "BUKRS" in learn_mappings(session, "default", exclude_project=slice_result["project_id"], include_org=True)["fields"]
    assert learn_mappings(session, "default", exclude_project=slice_result["project_id"])["fields"]["HKONT"]["entries"] == h["entries"]
    rules, lookups, review = learned_rules(L, existing_fields={"SAKNR"})
    ids = [r["id"] for r in rules]
    assert "learned-hkont" in ids and "learned-zterm" in ids and "learned-saknr" not in ids
    hk = next(r for r in rules if r["id"] == "learned-hkont")
    assert hk["lookup"] == "learned_hkont" and lookups["learned_hkont"] == h["entries"] and hk["learned"] is True and "Project Borealis - divestment / borealis-rules v1 (approved by approver)" in hk["description"] and hk["on_missing"] == "passthrough"
    assert any(line.startswith("HKONT: 1 value(s) mapped differently") for line in review)
    # the other tenant learns nothing from this one
    assert learn_mappings(session, "other-tenant")["fields"] == {}


def test_generate_with_learning_api_and_cli(client, tokens, session, slice_result, other_projects, capsys):
    pid, mid = slice_result["project_id"], slice_result["manifest_id"]
    plain = client.post(f"{API}/projects/{pid}/rulesets/generate", params={"manifest_id": mid}, headers=tokens["architect"]).json()
    assert "learned-hkont" not in plain["source_yaml"] and "learning" not in plain
    r = client.post(f"{API}/projects/{pid}/rulesets/generate", params={"manifest_id": mid, "learn": True}, headers=tokens["architect"])
    assert r.status_code == 200, r.text
    d = r.json()
    rs = parse_ruleset(d["source_yaml"])
    assert "learned-hkont" in [x["id"] for x in rs.rules] and rs.lookups["learned_hkont"] == {"140000": "12100000", "160000": "21100000"} and validate_ruleset(rs)["ok"]
    assert d["learning"]["approved_rulesets"] >= 2 and d["learning"]["proposed"] == ["learned-hkont", "learned-saknr", "learned-zterm"] and any("HKONT" in x for x in d["learning"]["review"])
    seen = client.get(f"{API}/projects/{pid}/rulesets/learned", headers=tokens["viewer"]).json()
    assert seen["fields"]["HKONT"]["conflicts"]["800000"] and seen["approved_rulesets"] >= 2
    # saved as a draft: the learned rules are pending decisions like any other
    saved = client.post(f"{API}/projects/{pid}/rulesets", json={"source_yaml": d["source_yaml"]}, headers=tokens["architect"]).json()
    grid = {x["id"]: x for x in client.get(f"{API}/rulesets/{saved['id']}/rules", headers=tokens["viewer"]).json()["rules"]}
    assert grid["learned-hkont"]["decision"] == "PENDING" and grid["learned-hkont"]["mapping"].startswith("lookup learned_hkont (2 entries)")
    from sdtf.cli import main

    assert main(["rules-learn", "--project", pid]) == 0
    out = capsys.readouterr().out
    assert "HKONT: 2 value(s)" in out and "conflicts: 800000" in out and "ZTERM: 3 value(s)" in out
    assert main(["rules-learn", "--project", pid, "--json"]) == 0 and '"learned_hkont"' not in capsys.readouterr().out
    assert main(["rules-learn", "--project", "nope"]) == 2

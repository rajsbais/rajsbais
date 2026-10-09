"""The 12 MVP steps through the HTTP API with role-based users."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from rfactory.api.main import create_app


def H(u):
    return {"X-Demo-User": u}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_authentication_and_authorization(client):
    assert client.get("/api/systems").status_code == 401
    assert client.get("/api/systems", headers=H("nobody")).status_code == 401
    assert client.post("/api/demo/bootstrap", headers=H("erin.auditor")).status_code == 403
    assert client.get("/api/audit", headers=H("bob.steward")).status_code == 403
    assert client.get("/api/health").json()["mode"] == "SIMULATED_SAP"
    me = client.get("/api/me", headers=H("refresh.copilot")).json()
    assert me["kind"] == "agent" and "plan:approve" not in me["permissions"]


def test_mvp_scenario_over_http(client):
    # 1 register + 2 discover
    b = client.post("/api/demo/bootstrap", headers=H("alice.basis")).json()
    sid, tid = b["source"]["id"], b["target"]["id"]
    d = client.get(f"/api/systems/{sid}/discovery", headers=H("bob.steward")).json()
    assert d["simulated"] and {c["code"] for c in d["company_codes"]} == {"1000", "2000"} and d["plants"]
    assert d["business_objects"]["sales_orders"] > 0
    assert client.get(f"/api/systems/{tid}/readiness", headers=H("bob.steward")).json()["ready"]
    combos = client.get("/api/landscape/combinations", headers=H("bob.steward")).json()
    assert all(not (c["target"] == sid and (c["selective"] or c["full_system_refresh"])) for c in combos)
    assert client.get("/api/objects/registry", headers=H("bob.steward")).json()["validation"]["ok"]

    # production target refused
    r = client.post("/api/projects", json={"name": "x", "source_id": tid, "target_id": sid}, headers=H("alice.basis"))
    assert r.status_code == 403

    # 3 select company code + date range
    pid = client.post("/api/projects", json={"name": "CC1000 90d", "source_id": sid, "target_id": tid},
                      headers=H("alice.basis")).json()["id"]
    r = client.put(f"/api/projects/{pid}/manifest", headers=H("alice.basis"), json={
        "scope": {"object_type": "SALES_ORDER", "company_codes": ["1000"]}, "last_days": 90,
        "include_downstream": ["DELIVERY", "BILLING", "FI_DOCUMENT"], "masking_policy_id": "gdpr-standard"})
    assert r.status_code == 200 and r.json()["manifest"]["version"] == 1

    # 4-6 associated data, dependencies, volume preview
    plan = client.post(f"/api/projects/{pid}/plan", headers=H("alice.basis")).json()
    assert plan["instances"] > 0 and plan["estimate"]["model_assumption"] and not plan["blocking"]
    inst = client.get(f"/api/projects/{pid}/instances?type=CUSTOMER", headers=H("bob.steward")).json()
    assert inst["total"] > 0 and inst["items"][0]["origin"] == "REQUIRED"

    # 7 conflicts (default policy blocks), 8 masking
    c = client.post(f"/api/projects/{pid}/conflicts/analyze", headers=H("alice.basis")).json()
    assert c["blocking"]
    assert client.post(f"/api/projects/{pid}/submit", headers=H("alice.basis")).status_code == 409
    adv = client.get(f"/api/projects/{pid}/agents/conflicts", headers=H("bob.steward")).json()
    assert adv["recommended_policy"] == "skip-differences" and adv["requires_human_approval"]
    bad = client.put(f"/api/projects/{pid}/conflicts/policy", headers=H("alice.basis"), json={"conflict_policy": {"DUPLICATE_DIFFERENT": "REMAP"}})
    assert bad.status_code == 422
    client.put(f"/api/projects/{pid}/conflicts/policy", headers=H("alice.basis"), json={"conflict_policy": {"DUPLICATE_DIFFERENT": "SKIP"}})
    client.post(f"/api/projects/{pid}/plan", headers=H("alice.basis"))
    m = client.get(f"/api/projects/{pid}/masking", headers=H("bob.steward")).json()
    assert m["uncovered"] == 1
    rules = client.get(f"/api/projects/{pid}/agents/masking", headers=H("bob.steward")).json()["add_rules"]
    r = client.post(f"/api/projects/{pid}/masking/rules", headers=H("dave.privacy"), json={"rules": rules})
    assert r.status_code == 200 and r.json()["uncovered"] == 0
    assert client.post(f"/api/projects/{pid}/conflicts/analyze", headers=H("alice.basis")).json()["blocking"] is False

    # 9 approved plan: SoD + roles
    assert client.post(f"/api/projects/{pid}/submit", headers=H("alice.basis")).json()["status"] == "PENDING_APPROVAL"
    assert client.post(f"/api/projects/{pid}/approve", json={}, headers=H("alice.basis")).status_code == 403
    assert client.post(f"/api/projects/{pid}/approve", json={}, headers=H("refresh.copilot")).status_code == 403
    assert client.post(f"/api/projects/{pid}/execute", headers=H("alice.basis")).status_code == 409
    assert client.post(f"/api/projects/{pid}/approve", json={"comment": "ok"}, headers=H("carol.approver")).json()["status"] == "APPROVED"

    # 10 execute, 11 validate, 12 reports
    run = client.post(f"/api/projects/{pid}/execute", headers=H("alice.basis"))
    assert run.status_code == 202 and run.json()["status"] == "COMPLETED" and run.json()["simulated"]
    rid = run.json()["id"]
    rec = client.get(f"/api/runs/{rid}/reconciliation", headers=H("erin.auditor")).json()
    assert rec["release"] == "RELEASED" and set(rec["summary"]) == {"technical", "business", "security"}
    assert "SIMULATED" in client.get(f"/api/runs/{rid}/report", headers=H("erin.auditor")).text
    ev = client.get(f"/api/runs/{rid}/evidence", headers=H("erin.auditor"))
    assert zipfile.ZipFile(io.BytesIO(ev.content)).testzip() is None
    assert client.get(f"/api/runs/{rid}/evidence", headers=H("bob.steward")).status_code == 403
    assert client.get("/api/audit/verify", headers=H("erin.auditor")).json()["valid"]
    actions = [e["action"] for e in client.get("/api/audit", headers=H("erin.auditor")).json()]
    for a in ("project.created", "plan.approved", "run.completed", "reconciliation.completed"):
        assert a in actions


def test_capability_matrix_is_honest(client):
    caps = client.get("/api/capabilities", headers=H("erin.auditor")).json()
    statuses = {c["status"] for c in caps}
    assert statuses <= {"implemented-simulated", "catalogued", "planned", "designed"}
    assert all(c["status"] != "production-ready" for c in caps)
    assert next(c for c in caps if c["module"].startswith("M2"))["status"] == "implemented-simulated"


def test_delta_refresh_over_http(client):
    b = client.post("/api/demo/bootstrap", headers=H("alice.basis")).json()
    sid, tid = b["source"]["id"], b["target"]["id"]
    sc = client.post(f"/api/delta/scenarios/demo?source_id={sid}&target_id={tid}", headers=H("alice.basis"))
    assert sc.status_code == 201
    scid = sc.json()["id"]
    assert client.post(f"/api/delta/scenarios/{scid}/run", json={}, headers=H("alice.basis")).status_code == 409  # not approved
    assert client.post(f"/api/delta/scenarios/{scid}/submit", headers=H("alice.basis")).json()["status"] == "PENDING_APPROVAL"
    assert client.post(f"/api/delta/scenarios/{scid}/approve", headers=H("alice.basis")).status_code == 403
    assert client.post(f"/api/delta/scenarios/{scid}/approve", headers=H("refresh.copilot")).status_code == 403
    ok = client.post(f"/api/delta/scenarios/{scid}/approve", headers=H("carol.approver")).json()
    assert ok["status"] == "APPROVED" and ok["next_due"]
    # the demo scenario lacks the Z-field masking rule: the first run must block, not leak
    r = client.post(f"/api/delta/scenarios/{scid}/run", json={}, headers=H("alice.basis")).json()
    assert r["status"] == "BLOCKED" and any("no masking rule" in x for x in r["blocking"])
    assert client.get(f"/api/delta/scenarios/{scid}/history", headers=H("erin.auditor")).json()[0]["status"] == "BLOCKED"
    rec = client.get(f"/api/delta/scenarios/{scid}/masking/recommended", headers=H("bob.steward")).json()
    assert [(x["table"], x["field"]) for x in rec] == [("KNA1", "ZZ_CONTACT_EMAIL")]
    assert client.post(f"/api/delta/scenarios/{scid}/masking/rules", json={"rules": rec}, headers=H("erin.auditor")).status_code == 403
    chg = client.post(f"/api/delta/scenarios/{scid}/masking/rules", json={"rules": rec}, headers=H("dave.privacy")).json()
    assert chg["status"] == "DRAFT" and chg["approval"] is None  # rules are part of what was approved
    client.post(f"/api/delta/scenarios/{scid}/submit", headers=H("alice.basis"))
    client.post(f"/api/delta/scenarios/{scid}/approve", headers=H("carol.approver"))
    first = client.post(f"/api/delta/scenarios/{scid}/run", json={}, headers=H("alice.basis")).json()
    assert first["status"] == "COMPLETED" and first["release"] == "RELEASED" and first["kind"] == "initial"
    chg_ev = client.post(f"/api/demo/simulate-source-changes?system_id={sid}", headers=H("alice.basis")).json()
    pv = client.post(f"/api/delta/scenarios/{scid}/preview", json={}, headers=H("alice.basis")).json()
    assert pv["new"] >= 3 and pv["changed"] >= 3 and pv["blocking"] == []
    second = client.post(f"/api/delta/scenarios/{scid}/run", json={}, headers=H("alice.basis")).json()
    assert second["release"] == "RELEASED" and second["from_seq"] == first["to_seq"] and second["to_seq"] == chg_ev["change_seq"]
    assert client.post("/api/delta/tick", json={}, headers=H("erin.auditor")).status_code == 403
    assert client.post("/api/delta/tick", json={}, headers=H("svc.scheduler")).status_code == 200
    assert client.post(f"/api/demo/simulate-source-changes?system_id={sid}", headers=H("erin.auditor")).status_code == 403
    assert client.post(f"/api/demo/simulate-source-changes?system_id={sid}", headers=H("alice.basis")).json()["simulated"]


def test_test_data_catalog_over_http(client):
    b = client.post("/api/demo/bootstrap", headers=H("alice.basis")).json()
    sid, tid = b["source"]["id"], b["target"]["id"]
    t = client.get("/api/tdm/templates", headers=H("tina.tester")).json()
    assert any(x["id"] == "o2c_complete" for x in t["available"]) and any(x["id"] == "make_to_stock" for x in t["planned"])
    cands = client.get(f"/api/tdm/templates/o2c_complete/candidates?source_id={sid}&days=90", headers=H("tina.tester")).json()
    assert cands and cands[0]["attrs"]["company_code"] == "1000"

    body = {"target_id": tid, "template_id": "o2c_complete", "mode": "subset", "count": 1, "params": {"days": 90}, "reserve": True}
    assert client.post("/api/tdm/requests", json=body, headers=H("tina.tester")).json()["status"] == "REJECTED"   # no policy yet
    assert client.post("/api/tdm/requests", json=body, headers=H("erin.auditor")).status_code == 403
    pol = client.post("/api/tdm/policies", json={"name": "QA self-service", "source_id": sid, "target_id": tid}, headers=H("tina.tester"))
    assert pol.status_code == 403                                                                                  # testers cannot define policy
    pid = client.post("/api/tdm/policies", json={"name": "QA self-service", "source_id": sid, "target_id": tid}, headers=H("alice.basis")).json()["id"]
    assert client.post(f"/api/tdm/policies/{pid}/submit", headers=H("alice.basis")).json()["status"] == "PENDING_APPROVAL"
    for who in ("alice.basis", "refresh.copilot", "tina.tester"):
        assert client.post(f"/api/tdm/policies/{pid}/approve", headers=H(who)).status_code == 403
    assert client.post(f"/api/tdm/policies/{pid}/approve", headers=H("carol.approver")).json()["status"] == "ACTIVE"

    # a CI pipeline (service account) requests data, reads handles, uses them, releases
    r = client.post("/api/tdm/requests", json=body, headers=H("svc.ci")).json()
    assert r["status"] == "FULFILLED" and r["datasets"][0]["handles"]["sales_order"]
    did = r["datasets"][0]["id"]
    h = client.get(f"/api/tdm/datasets/{did}/handles", headers=H("svc.ci")).json()
    assert h["state"] == "RESERVED" and h["handles"]["billing_documents"]
    assert client.post(f"/api/tdm/datasets/{did}/reserve", json={}, headers=H("tom.tester")).status_code == 409
    u = client.post(f"/api/tdm/datasets/{did}/usage", json={"test_case": {"system": "jenkins", "id": "build-381", "title": "O2C regression"},
                                                             "outcome": "passed"}, headers=H("svc.ci")).json()
    assert u["usage"][0]["outcome"] == "passed"
    assert client.get("/api/tdm/catalog?test_case=build-381", headers=H("tina.tester")).json()[0]["id"] == did
    assert client.post(f"/api/tdm/datasets/{did}/golden", headers=H("svc.ci")).status_code == 403                  # curation is not self-service
    assert client.post(f"/api/tdm/datasets/{did}/release", json={}, headers=H("svc.ci")).json()["state"] == "AVAILABLE"
    assert client.get(f"/api/tdm/datasets/{did}/verify", headers=H("tina.tester")).json()["intact"]
    assert client.post(f"/api/tdm/datasets/{did}/purge", json={}, headers=H("tina.tester")).status_code == 403
    assert client.post(f"/api/tdm/datasets/{did}/purge", json={}, headers=H("refresh.copilot")).status_code == 403
    assert client.post(f"/api/tdm/datasets/{did}/purge", json={}, headers=H("carol.approver")).status_code == 409  # still AVAILABLE
    assert client.post("/api/tdm/sweep", headers=H("tina.tester")).status_code == 403
    assert client.post("/api/tdm/sweep", headers=H("svc.scheduler")).status_code == 200
    assert client.post(f"/api/tdm/scan?target_id={tid}", headers=H("tina.tester")).status_code == 403
    assert client.post(f"/api/tdm/scan?target_id={tid}", headers=H("bob.steward")).status_code == 200
    assert client.get("/api/audit/verify", headers=H("erin.auditor")).json()["valid"]


def test_lean_client_builder_over_http(client):
    b = client.post("/api/demo/bootstrap", headers=H("alice.basis")).json()
    sid, tid = b["source"]["id"], b["target"]["id"]
    prof = client.get("/api/lean/profiles", headers=H("tina.tester")).json()
    assert len(prof["workflows"]) == 3 and "066" in prof["reserved_clients"]
    presets = client.get(f"/api/lean/presets?source_id={sid}", headers=H("alice.basis")).json()
    assert {p["purpose"] for p in presets} == {"training", "sandbox", "functional", "regression"}
    spec = next(p for p in presets if p["purpose"] == "functional")
    assert client.post("/api/lean/templates", json=spec, headers=H("tina.tester")).status_code == 403
    t = client.post("/api/lean/templates", json={k: spec[k] for k in ("name", "source_id", "purpose", "masters", "transactions", "masking_policy_id",
                                                                       "retention_days", "max_rows")} | {"company_codes": ["1000"]},
                    headers=H("alice.basis")).json()
    tpl = t["id"]
    body = {"template_id": tpl, "host_id": tid, "client": "320"}
    assert client.post("/api/lean/builds", json=body, headers=H("alice.basis")).status_code == 409           # not approved
    client.post(f"/api/lean/templates/{tpl}/submit", headers=H("alice.basis"))
    for who in ("alice.basis", "refresh.copilot", "tina.tester"):
        assert client.post(f"/api/lean/templates/{tpl}/approve", headers=H(who)).status_code == 403
    assert client.post(f"/api/lean/templates/{tpl}/approve", headers=H("carol.approver")).json()["status"] == "APPROVED"
    est = client.post(f"/api/lean/templates/{tpl}/estimate", json={"host_id": tid}, headers=H("tina.tester")).json()
    assert est["savings"]["rows_pct"] > 50 and est["duration"]["lean"]["model_assumption"]
    assert client.post("/api/lean/builds", json=body, headers=H("tina.tester")).status_code == 403
    assert client.post("/api/lean/builds", json={**body, "client": "066"}, headers=H("alice.basis")).status_code == 409
    assert client.post("/api/lean/builds", json={**body, "host_id": sid}, headers=H("alice.basis")).status_code == 403   # production host
    r = client.post("/api/lean/builds", json=body, headers=H("alice.basis"))
    assert r.status_code == 201 and r.json()["status"] == "READY", r.json()
    build = r.json()
    assert all(c["status"] == "pass" for c in build["checks"])
    sysl = client.get("/api/systems", headers=H("bob.steward")).json()
    new = next(s for s in sysl if s["id"] == build["system_id"])
    assert new["client"] == "320" and new["role"] == "QAS" and new["writable_target"]
    assert [c["id"] for c in client.get("/api/lean/clients", headers=H("tina.tester")).json()] == [build["id"]]
    assert client.post(f"/api/lean/clients/{build['system_id']}/protection", json={"locked": True}, headers=H("tina.tester")).status_code == 403
    assert client.post(f"/api/lean/clients/{build['system_id']}/protection", json={"locked": True}, headers=H("bob.steward")).json()["locked"]
    assert client.post(f"/api/lean/builds/{build['id']}/decommission", headers=H("alice.basis")).status_code == 403
    assert client.post(f"/api/lean/builds/{build['id']}/decommission", headers=H("refresh.copilot")).status_code == 403
    assert client.post(f"/api/lean/builds/{build['id']}/decommission", headers=H("carol.approver")).status_code == 200
    assert client.post("/api/lean/sweep", headers=H("svc.scheduler")).status_code == 200
    assert client.get("/api/audit/verify", headers=H("erin.auditor")).json()["valid"]


def test_post_copy_factory_over_http(client):
    b = client.post("/api/demo/bootstrap", headers=H("alice.basis")).json()
    sid, tid = b["source"]["id"], b["target"]["id"]
    tasks = client.get("/api/postcopy/tasks", headers=H("tina.tester")).json()
    assert len(tasks) == 17 and all(t["status"].startswith("implemented") for t in tasks)
    assert client.get(f"/api/postcopy/systems/{tid}/assessment", headers=H("tina.tester")).json()["active_production_references"] == 0
    assert client.post("/api/postcopy/profiles", json={"system_id": sid, "name": "p"}, headers=H("alice.basis")).status_code == 403   # production
    pid = client.post("/api/postcopy/profiles", json={"system_id": tid, "name": "EQ1 pre-copy"}, headers=H("alice.basis")).json()["id"]
    client.post(f"/api/postcopy/profiles/{pid}/submit", headers=H("alice.basis"))
    assert client.post(f"/api/postcopy/profiles/{pid}/approve", headers=H("alice.basis")).status_code == 403
    assert client.post(f"/api/postcopy/profiles/{pid}/approve", headers=H("carol.approver")).json()["status"] == "APPROVED"
    assert client.post(f"/api/demo/simulate-system-copy?source_id={sid}&target_id={tid}", headers=H("tina.tester")).status_code == 403
    cp = client.post(f"/api/demo/simulate-system-copy?source_id={sid}&target_id={tid}", headers=H("alice.basis")).json()
    assert cp["assessment"]["active_production_references"] > 20 and cp["simulated"]
    assert client.post(f"/api/demo/simulate-system-copy?source_id={tid}&target_id={sid}", headers=H("alice.basis")).status_code == 403
    run = client.post("/api/postcopy/runs", json={"target_id": tid, "source_id": sid, "profile_id": pid}, headers=H("alice.basis")).json()
    rid = run["id"]
    assert run["status"] == "AWAITING_APPROVAL" and set(run["required_approvals"]) == {"basis_lead", "integration_owner", "security_officer"}
    assert client.post(f"/api/postcopy/runs/{rid}/execute", headers=H("alice.basis")).status_code == 409
    who = {"basis_lead": "bastian.lead", "integration_owner": "ingrid.integration", "security_officer": "sven.security"}
    assert client.post(f"/api/postcopy/runs/{rid}/approvals", json={"label": "security_officer"}, headers=H("ingrid.integration")).status_code == 403
    assert client.post(f"/api/postcopy/runs/{rid}/approvals", json={"label": "basis_lead"}, headers=H("refresh.copilot")).status_code == 403
    for lab, user in who.items():
        r = client.post(f"/api/postcopy/runs/{rid}/approvals", json={"label": lab}, headers=H(user))
        assert r.status_code == 200, r.text
    assert client.post(f"/api/postcopy/runs/{rid}/execute", headers=H("tina.tester")).status_code == 403
    done = client.post(f"/api/postcopy/runs/{rid}/execute", headers=H("alice.basis")).json()
    assert done["status"] == "COMPLETED" and done["gate"]["ok"] and done["simulated"]
    assert client.get(f"/api/postcopy/systems/{tid}/assessment?profile_id={pid}", headers=H("tina.tester")).json()["active_production_references"] == 0
    assert client.get(f"/api/postcopy/systems/{tid}/gate?profile_id={pid}", headers=H("tina.tester")).json()["ok"]
    ev = client.get(f"/api/postcopy/runs/{rid}/evidence", headers=H("sven.security"))
    assert ev.status_code == 200 and zipfile.ZipFile(io.BytesIO(ev.content)).testzip() is None
    assert client.get(f"/api/postcopy/runs/{rid}/evidence", headers=H("tina.tester")).status_code == 403
    assert client.post(f"/api/postcopy/runs/{rid}/rollback", headers=H("refresh.copilot")).status_code == 403
    assert client.post(f"/api/postcopy/runs/{rid}/rollback", headers=H("bastian.lead")).json()["status"] == "ROLLED_BACK"
    plan = client.get(f"/api/full-refresh/plan?source_id={sid}&target_id={tid}", headers=H("tina.tester")).json()
    assert all(p["status"] == "executable-simulated" for p in plan["phases"] if p["no"] != 7)
    assert client.get("/api/audit/verify", headers=H("erin.auditor")).json()["valid"]


def test_ai_agents_over_http(client):
    boot = client.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    cat = client.get("/api/agents", headers=H("erin.auditor")).json()
    assert len(cat["agents"]) == 12
    assert client.post("/api/agents/nope/run", json={}, headers=H("alice.basis")).status_code == 409
    assert client.post("/api/agents/masking-recommendation/run", json={}, headers=H("alice.basis")).status_code == 422
    r = client.post("/api/agents/landscape-discovery/run", json={}, headers=H("erin.auditor"))
    assert r.status_code == 200
    rec = next(x for x in r.json()["recommendations"] if x["action"]["kind"] == "lock_system")
    assert client.get(f"/api/agents/reports/{r.json()['id']}", headers=H("erin.auditor")).json()["id"] == r.json()["id"]
    assert client.get("/api/agents/reports/rpt-none", headers=H("erin.auditor")).status_code == 404
    # agents and unauthorised humans cannot apply
    assert client.post(f"/api/agents/recommendations/{rec['id']}/apply", headers=H("refresh.copilot")).status_code == 403
    assert client.post(f"/api/agents/recommendations/{rec['id']}/apply", headers=H("erin.auditor")).status_code == 403
    ok = client.post(f"/api/agents/recommendations/{rec['id']}/apply", headers=H("alice.basis"))
    assert ok.status_code == 200 and ok.json()["status"] == "APPLIED"
    assert client.post(f"/api/agents/recommendations/{rec['id']}/apply", headers=H("alice.basis")).status_code == 409
    assert client.get("/api/agents/recommendations?status=APPLIED", headers=H("erin.auditor")).json()[0]["id"] == rec["id"]
    c = client.post("/api/agents/copilot", json={"question": "is everything compliant?"}, headers=H("erin.auditor")).json()
    assert c["routed_to"] == "compliance-verification" and c["report"]["artifacts"]["controls"]

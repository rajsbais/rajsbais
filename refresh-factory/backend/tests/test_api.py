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
    assert next(c for c in caps if c["module"].startswith("M2"))["status"] == "catalogued"


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

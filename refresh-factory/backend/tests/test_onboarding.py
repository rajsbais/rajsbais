"""Connecting a system from the UI/API: the read-only smoke test that runs before registering anything."""
import json

import pytest
from fastapi.testclient import TestClient

from rfactory.api.main import create_app

H = lambda u: {"X-Demo-User": u}


def body(kind="rfc", **over):
    sysd = {"sid": "SBX1", "client": "100", "role": "SBX", "owner": "me"}
    prof = ({"name": "t", "kind": "rfc", "ashost": "sandbox.invalid", "sysnr": "00", "client": "100", "user": "RFREAD", "password_ref": "env:ONB_PW", "calls_per_minute": 60000}
            if kind == "rfc" else
            {"name": "t", "kind": "odata", "base_url": "https://sandbox.invalid:44300", "user": "RFREAD", "password_ref": "env:ONB_PW", "calls_per_minute": 60000})
    return {"system": sysd, "profile": prof, "max_rows": 100, "confirm": True, **over}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("RFACTORY_ALLOW_FAKE_ENDPOINTS", "1")
    monkeypatch.setenv("ONB_PW", "s3cret-never-shown")
    c = TestClient(create_app(tmp_path, persist=False))
    c.post("/api/demo/bootstrap", headers=H("root.admin"))
    return c


@pytest.mark.parametrize("kind", ["rfc", "odata"])
def test_a_smoke_test_reports_without_registering_the_system(api, kind):
    r = api.post("/api/systems/smoke", json=body(kind), headers=H("alice.basis"))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["connection"]["ok"] and rep["read_only"] and rep["tables"]["MARA"]["status"] == "ok"
    assert "s3cret-never-shown" not in r.text
    assert [s["sid"] for s in api.get("/api/systems", headers=H("alice.basis")).json()].count("SBX1") == 0  # nothing registered
    audit = [e for e in api.app.state.svc.audit.entries() if e["action"] == "system.smoke"]
    assert len(audit) == 1 and audit[0]["details"]["connected"] is True and "rows_read" not in json.dumps(audit[0])
    assert "s3cret-never-shown" not in json.dumps(audit)


def test_confirmation_permission_and_scope_are_required(api):
    assert api.post("/api/systems/smoke", json=body(confirm=False), headers=H("alice.basis")).status_code == 422
    for who in ("refresh.copilot", "tina.tester", "svc.scheduler"):
        assert api.post("/api/systems/smoke", json=body(), headers=H(who)).status_code == 403, who
    from rfactory.sap.adapter import SapSystem
    from rfactory.sap.connectors.profile import ConnectionProfile
    from rfactory.security.auth import Forbidden, Principal
    svc = api.app.state.svc
    scoped = Principal("scoped", "scoped", ("basis",), attrs={"systems": ["EP1"]})
    prof = ConnectionProfile(**body()["profile"])
    with pytest.raises(Forbidden, match="outside your scope"):
        svc.smoke_remote(scoped, SapSystem(sid="S4X", client="100", role="SBX"), prof)
    assert svc.smoke_remote(scoped, SapSystem(sid="EP1", client="100", role="SBX"), prof)["connection"]["ok"]


def test_bad_input_is_explained_not_executed(api, monkeypatch):
    monkeypatch.delenv("ONB_PW")
    r = api.post("/api/systems/smoke", json=body(), headers=H("alice.basis"))
    assert r.status_code == 409 and "ONB_PW" in r.text and "not set" in r.text
    monkeypatch.setenv("ONB_PW", "x")
    for over in ({"max_rows": 0}, {"max_rows": 100000}):
        assert api.post("/api/systems/smoke", json=body(**over), headers=H("alice.basis")).status_code == 409
    plain = body()
    plain["profile"]["password_ref"] = "hunter2"
    assert api.post("/api/systems/smoke", json=plain, headers=H("alice.basis")).status_code == 409  # plain-text secrets are refused
    http = body("odata")
    http["profile"]["base_url"] = "http://evil.example"
    assert api.post("/api/systems/smoke", json=http, headers=H("alice.basis")).status_code == 409
    assert api.post("/api/systems/smoke", json={"system": {"bogus": 1}, "profile": {}, "confirm": True}, headers=H("alice.basis")).status_code == 422


def test_the_fake_endpoint_hook_is_off_by_default_so_a_real_attempt_is_made(tmp_path, monkeypatch):
    monkeypatch.delenv("RFACTORY_ALLOW_FAKE_ENDPOINTS", raising=False)
    monkeypatch.setenv("ONB_PW", "x")
    c = TestClient(create_app(tmp_path, persist=False))
    r = c.post("/api/systems/smoke", json=body("rfc"), headers=H("alice.basis"))
    assert r.status_code == 409 and "pyrfc" in r.text  # the real RFC transport was demanded; no fake was substituted


def test_registering_after_a_good_smoke_test_gives_a_read_only_source(api):
    assert api.post("/api/systems/smoke", json=body(), headers=H("alice.basis")).status_code == 200
    r = api.post("/api/systems/connect", json={"system": body()["system"], "profile": body()["profile"]}, headers=H("alice.basis"))
    assert r.status_code == 201 and r.json()["writable_target"] is False
    info = api.get(f"/api/systems/{r.json()['id']}/remote", headers=H("alice.basis")).json()
    assert info["capabilities"]["writes"] is False


def test_even_with_the_hook_on_only_hosts_that_cannot_exist_are_faked(api):
    real = body("rfc")
    real["profile"]["ashost"] = "sap.corp.example"
    r = api.post("/api/systems/smoke", json=real, headers=H("alice.basis"))
    assert r.status_code == 409 and "pyrfc" in r.text  # a real-looking host is never served by a fake

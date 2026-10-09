"""HTTP hardening (security headers, body limit) and the production switch."""
import pytest
from fastapi.testclient import TestClient

from rfactory.api import hardening
from rfactory.api.main import create_app
from rfactory.security.oidc import AuthConfig

LOGIN = dict(authorize_url="https://idp.example/authorize", token_url="https://idp.example:8443/token", client_id="ui")


def client(tmp_path, **kw):
    return TestClient(create_app(tmp_path, persist=False, **kw))


def test_every_answer_carries_the_security_headers_and_api_answers_are_not_cached(tmp_path):
    c = client(tmp_path)
    for path in ("/api/health", "/api/auth/config", "/", "/api/me"):  # includes a 401 and the static UI
        r = c.get(path, headers={"X-Demo-User": "alice.basis"} if path == "/api/me" else {})
        h = r.headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
        assert "camera=()" in h["permissions-policy"] and h["cross-origin-opener-policy"] == "same-origin"
        assert "strict-transport-security" not in h  # only when asked
        if path.startswith("/api/"):
            assert h["cache-control"] == "no-store"
    r = c.get("/api/me")
    assert r.status_code == 401 and r.headers["x-frame-options"] == "DENY"  # errors too


def test_csp_never_allows_inline_scripts_or_other_origins_without_a_configured_login(tmp_path):
    p = hardening.csp(AuthConfig(mode="demo"))
    assert "'unsafe-eval'" not in p and "script-src 'self'" in p and "connect-src 'self'" in p and "http" not in p.split("connect-src")[1].split(";")[0]
    assert "script-src 'self' 'unsafe-inline'" not in p and "object-src 'none'" in p


def test_csp_allows_the_identity_providers_token_endpoint_only_when_login_is_configured(tmp_path):
    cfg = AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x", **LOGIN)
    p = hardening.csp(cfg)
    assert "connect-src 'self' https://idp.example:8443" in p
    assert "idp.example/authorize" not in p  # navigation to the authorize URL is not a connect-src matter
    paste = AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x")
    assert "idp.example" not in hardening.csp(paste)
    c = client(tmp_path, auth=cfg, jwks={"keys": []})
    assert "https://idp.example:8443" in c.get("/api/health").headers["content-security-policy"]


def test_hsts_only_when_switched_on(tmp_path, monkeypatch):
    monkeypatch.setenv("RFACTORY_HSTS", "1")
    r = client(tmp_path).get("/api/health")
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_a_large_body_is_refused_before_it_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(hardening, "MAX_BODY_BYTES", 1000)
    c = client(tmp_path)
    r = c.post("/api/projects", content=b"x" * 5000, headers={"X-Demo-User": "alice.basis", "Content-Type": "application/json"})
    assert r.status_code == 413 and "larger than" in r.json()["detail"] and r.headers["x-frame-options"] == "DENY"
    ok = c.post("/api/projects", json={"name": "n", "source_id": "a", "target_id": "b"}, headers={"X-Demo-User": "alice.basis"})
    assert ok.status_code in (404, 409, 422)  # reached the application


# ---------------------------------------------------------------- production switch
GOOD = {"RFACTORY_STATE_KEY": "k", "RFACTORY_AUDIT_KEY": "a"}


def test_production_problems_name_every_misconfiguration():
    demo = AuthConfig(mode="demo")
    probs = hardening.production_problems(demo, False, env={})
    text = " | ".join(probs)
    assert len(probs) == 4 and "demo header" in text and "RFACTORY_DATA_DIR" in text and "RFACTORY_STATE_KEY" in text and "RFACTORY_AUDIT_KEY" in text
    oidc = AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x")
    assert hardening.production_problems(oidc, True, env=GOOD) == []
    assert any("test hook" in p for p in hardening.production_problems(oidc, True, env={**GOOD, "RFACTORY_ALLOW_FAKE_ENDPOINTS": "1"}))
    assert any("RFACTORY_STATE_KEY" in p for p in hardening.production_problems(oidc, True, env={"RFACTORY_AUDIT_KEY": "a"}))


def test_production_refuses_to_start_when_misconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("RFACTORY_ENV", "production")
    with pytest.raises(RuntimeError, match="refuses to start.*demo header.*RFACTORY_DATA_DIR"):
        create_app(tmp_path, persist=False)
    with pytest.raises(RuntimeError, match="RFACTORY_STATE_KEY"):
        create_app(tmp_path / "d", persist=True, auth=AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x"), jwks={"keys": []})


def test_a_correctly_configured_production_start_works_and_switches_the_demo_endpoints_off(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("RFACTORY_ENV", "production")
    monkeypatch.setenv("RFACTORY_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RFACTORY_AUDIT_KEY", "audit-secret")
    c = TestClient(create_app(tmp_path, persist=True, auth=AuthConfig(mode="oidc", issuer="i", audience="a", jwks_file="x"), jwks={"keys": []}))
    assert c.get("/api/health").status_code == 200
    for path in ("/api/demo/bootstrap", "/api/demo/connect-fake-rfc", "/api/demo/simulate-source-changes"):
        r = c.post(path, headers={"Authorization": "Bearer x"})
        assert r.status_code == 404 and "disabled in production" in r.json()["detail"]


def test_outside_production_the_demo_endpoints_stay_available(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/demo/bootstrap", headers={"X-Demo-User": "root.admin"}).status_code == 200

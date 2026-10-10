"""Revoking bearer tokens (single token by jti, everything a subject holds by cutoff), in oidc mode."""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rfactory.api.main import create_app
from rfactory.security.oidc import AuthConfig, AuthError
from rfactory.security.revocation import RevocationList

from .test_auth import AUD, ISS, bearer, idp, jwk_of, rsa_key, token  # noqa: F401  (idp is a fixture)


def tok(idp, **kw):
    kw.setdefault("jti", uuid.uuid4().hex)
    return token(idp.key, **kw)


def me(idp, t):
    return idp.client.get("/api/me", headers=bearer(t))


# ---------------------------------------------------------------- the list itself
def test_a_revoked_jti_is_refused_and_forgotten_after_the_token_would_have_expired():
    r = RevocationList()
    now = 10_000.0
    r.revoke_token("j1", now + 300, "sec", now=now)
    with pytest.raises(AuthError) as e:
        r.check({"jti": "j1", "sub": "u", "iat": now})
    assert e.value.reason == "revoked"
    r.check({"jti": "j2", "sub": "u", "iat": now})  # another token is untouched
    r.prune(now + 1000)
    assert r.jtis == {}


def test_a_subject_cutoff_refuses_older_tokens_and_tokens_that_cannot_prove_they_are_newer():
    r = RevocationList()
    r.revoke_subject("u", "sec", now=1000.0)
    for claims in ({"sub": "u", "iat": 999}, {"sub": "u", "iat": 1000}, {"sub": "u"}, {"sub": "u", "iat": "x"}):
        with pytest.raises(AuthError):
            r.check(claims)
    r.check({"sub": "u", "iat": 1001})
    r.check({"sub": "someone-else", "iat": 1})


# ---------------------------------------------------------------- over HTTP
def test_logout_revokes_this_token_only(idp):
    a, b = tok(idp), tok(idp)
    assert me(idp, a).status_code == 200 and me(idp, b).status_code == 200
    r = idp.client.post("/api/auth/logout", headers=bearer(a))
    assert r.json() == {"revoked": True}
    bad = me(idp, a)
    assert bad.status_code == 401 and "revoked" in bad.json()["detail"]
    assert me(idp, b).status_code == 200  # a second session of the same person is untouched


def test_logout_all_revokes_everything_issued_up_to_now_but_not_a_newer_login(idp):
    old1, old2 = tok(idp), tok(idp, iat=int(time.time()) - 5)
    assert idp.client.post("/api/auth/logout-all", headers=bearer(old1)).json()["revoked"] is True
    assert me(idp, old1).status_code == 401 and me(idp, old2).status_code == 401
    new = tok(idp, iat=int(time.time()) + 10)  # issued after the cutoff (within the clock leeway)
    assert me(idp, new).status_code == 200
    other = tok(idp, sub="u-2")
    assert me(idp, other).status_code == 200  # other people are untouched


def test_a_token_without_jti_cannot_be_revoked_individually(idp):
    t = token(idp.key)  # no jti
    r = idp.client.post("/api/auth/logout", headers=bearer(t))
    assert r.json()["revoked"] is False and "logout-all" in r.json()["reason"]
    assert me(idp, t).status_code == 200
    idp.client.post("/api/auth/logout-all", headers=bearer(t))
    assert me(idp, t).status_code == 401


def test_require_jti_refuses_tokens_that_could_not_be_revoked(tmp_path):
    cfg = AuthConfig(mode="oidc", issuer=ISS, audience=AUD, require_jti=True)
    key = rsa_key()
    c = TestClient(create_app(tmp_path, persist=False, auth=cfg, jwks={"keys": [jwk_of(key, "k1")]}))
    assert c.get("/api/me", headers=bearer(token(key))).status_code == 401
    assert c.get("/api/me", headers=bearer(token(key, jti="x1"))).status_code == 200


def test_a_security_officer_can_revoke_a_subject_or_a_token_and_nobody_else_can(idp):
    victim = tok(idp, sub="victim", roles=["basis"])
    officer = tok(idp, sub="sec-1", roles=["security_officer"])
    steward = tok(idp, sub="ds-1", roles=["data_steward"])
    agent = tok(idp, sub="bot", roles=["security_officer"], rf_kind="agent")
    body = {"subject": "victim", "reason": "laptop lost"}
    for who in (steward, agent):
        assert idp.client.post("/api/auth/revoke", json=body, headers=bearer(who)).status_code == 403
    assert me(idp, victim).status_code == 200
    r = idp.client.post("/api/auth/revoke", json=body, headers=bearer(officer))
    assert r.status_code == 200 and me(idp, victim).status_code == 401
    other = tok(idp, sub="victim2", roles=["basis"], jti="j-known")
    assert idp.client.post("/api/auth/revoke", json={"jti": "j-known", "reason": "stolen"}, headers=bearer(officer)).status_code == 200
    assert me(idp, other).status_code == 401
    assert idp.client.post("/api/auth/revoke", json={}, headers=bearer(officer)).status_code == 422
    assert idp.client.post("/api/auth/revoke", json={"subject": "a", "jti": "b"}, headers=bearer(officer)).status_code == 422
    lst = idp.client.get("/api/auth/revocations", headers=bearer(officer)).json()
    assert [s["subject"] for s in lst["subjects"]] == ["victim"] and len(lst["tokens"]) == 1
    assert idp.client.get("/api/auth/revocations", headers=bearer(steward)).status_code == 403
    actions = [e["action"] for e in idp.app.state.svc.audit.entries()]
    assert actions.count("auth.revoked") == 2


def test_revocations_survive_a_restart(tmp_path):
    cfg = AuthConfig(mode="oidc", issuer=ISS, audience=AUD)
    key = rsa_key()
    jwks = {"keys": [jwk_of(key, "k1")]}
    c1 = TestClient(create_app(tmp_path, persist=True, auth=cfg, jwks=jwks))
    t = token(key, jti="persist-1")
    assert c1.get("/api/me", headers=bearer(t)).status_code == 200
    c1.post("/api/auth/logout", headers=bearer(t))
    c1.post("/api/auth/logout-all", headers=bearer(token(key, sub="u-9", jti="x")))
    c2 = TestClient(create_app(tmp_path, persist=True, auth=cfg, jwks=jwks))  # a fresh process on the same data directory
    assert c2.get("/api/me", headers=bearer(t)).status_code == 401
    assert c2.get("/api/me", headers=bearer(token(key, sub="u-9", jti="y", iat=int(time.time()) - 60))).status_code == 401
    assert c2.get("/api/me", headers=bearer(token(key, sub="u-8", jti="z"))).status_code == 200


def test_demo_mode_has_no_tokens_to_revoke(tmp_path):
    c = TestClient(create_app(tmp_path, persist=False))
    H = {"X-Demo-User": "sven.security"}
    assert c.post("/api/auth/revoke", json={"subject": "x"}, headers=H).status_code == 409
    assert c.post("/api/auth/logout-all", headers=H).status_code == 409


def test_an_agent_can_never_hold_the_revoke_permission():
    from rfactory.security.auth import Principal
    assert Principal("a", "a", ("security_officer",)).can("token:revoke")
    assert not Principal("a", "a", ("security_officer",), kind="agent").can("token:revoke")

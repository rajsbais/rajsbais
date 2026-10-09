import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from rfactory.api.main import create_app
from rfactory.security import authz
from rfactory.security.auth import DEMO_USERS as U, Forbidden, Principal
from rfactory.security.oidc import AuthConfig, AuthError, OidcVerifier
from rfactory.selective.manifest import Scope

from .conftest import ADMIN, ALICE, make_project
from .test_delta import approved as approved_delta

CORA = U["cora.regional"]
ISS, AUD = "https://idp.example.test", "keystone"


# ======================================================================== ABAC
def scoped_project(svc, user=CORA, company="2000"):
    p = svc.create_project(user, "regional", svc.src_id, svc.tgt_id)
    svc.set_manifest(user, p.id, Scope(object_type="SALES_ORDER", company_codes=[company]), ["DELIVERY", "BILLING", "FI_DOCUMENT"], "gdpr-standard", {})
    return p


def test_scoped_user_works_inside_the_scope_and_is_refused_outside_it(svc):
    p = scoped_project(svc)
    svc.build_plan(CORA, p.id)
    with pytest.raises(Forbidden, match="outside your scope"):
        svc.create_project(CORA, "x", svc.s4_src, svc.s4_tgt)  # systems not in the scope
    with pytest.raises(Forbidden, match="company code"):
        svc.set_manifest(CORA, p.id, Scope(object_type="SALES_ORDER", company_codes=["1000"]), [], "gdpr-standard", {})
    with pytest.raises(Forbidden, match="unbounded"):
        svc.set_manifest(CORA, p.id, Scope(object_type="SALES_ORDER"), [], "gdpr-standard", {})  # 'everything' is never inside a bounded scope
    with pytest.raises(Forbidden):
        svc.set_manifest(CORA, p.id, Scope(object_type="SALES_ORDER", company_codes=["2000", "1000"]), [], "gdpr-standard", {})


def test_a_real_plan_that_drags_in_another_companys_plant_is_refused(svc):
    c1000 = Principal("c1", "c1", ("data_steward",), attrs={"company_codes": ["1000"], "systems": ["EP1", "EQ1"]})
    p = svc.create_project(c1000, "cross-company", svc.src_id, svc.tgt_id)
    svc.set_manifest(c1000, p.id, Scope(object_type="SALES_ORDER", company_codes=["1000"]), ["DELIVERY", "BILLING", "FI_DOCUMENT"], "gdpr-standard", {})
    with pytest.raises(Forbidden, match=r"plant\(s\) \['2000'\].*\['2000'\]"):  # company-1000 orders ship from plant 2000, which belongs to company 2000
        svc.build_plan(c1000, p.id)
    assert p.plan is None
    ok = scoped_project(svc)  # company 2000 only needs plant 2000
    svc.build_plan(CORA, ok.id)
    svc.build_plan(ALICE, p.id)  # an unrestricted steward may build the very same plan


def test_unrestricted_users_are_unaffected(svc):
    p = make_project(svc, company=None, days=None)  # no company codes at all: allowed for an unrestricted steward
    svc.build_plan(ALICE, p.id)
    assert not authz.restricted(ALICE) and authz.restricted(CORA)


def test_plan_dependencies_cannot_smuggle_in_another_company(svc):
    class FakePlan:
        config_refs = {"COMPANY_CODE": {"2000", "1000"}}
    with pytest.raises(Forbidden, match=r"\['1000'\]"):
        authz.require_plan(CORA, FakePlan())
    FakePlan.config_refs = {"COMPANY_CODE": {"2000"}}
    authz.require_plan(CORA, FakePlan())
    authz.require_plan(ALICE, type("P", (), {"config_refs": {"COMPANY_CODE": {"9999"}}})())


def test_empty_attribute_lists_mean_nothing_is_allowed(svc):
    nobody = Principal("n", "n", ("data_steward",), attrs={"systems": [], "company_codes": []})
    with pytest.raises(Forbidden):
        svc.create_project(nobody, "x", svc.src_id, svc.tgt_id)
    only_systems = Principal("o", "o", ("data_steward",), attrs={"systems": ["EP1/100", "EQ1/200"]})  # SID/client form, no company restriction
    p = svc.create_project(only_systems, "x", svc.src_id, svc.tgt_id)
    svc.set_manifest(only_systems, p.id, Scope(object_type="SALES_ORDER", company_codes=["1000"]), [], "gdpr-standard", {})


def test_delta_tdm_lean_postcopy_fullrefresh_and_jobs_respect_the_system_scope(svc):
    from .test_delta import spec
    with pytest.raises(Forbidden):
        svc.delta.create(CORA, spec(svc, "S4"))
    with pytest.raises(Forbidden, match="company"):
        svc.delta.create(CORA, spec(svc))  # the demo scenario selects company 1000
    with pytest.raises(Forbidden):
        svc.postcopy.capture_profile(CORA, svc.s4_tgt, "x")
    scoped_tester = Principal("t", "t", ("tester",), attrs={"systems": ["EQ1"]})
    with pytest.raises(Forbidden):
        svc.tdm.request(scoped_tester, {"target_id": svc.s4_tgt, "template_id": "x"})
    sc = approved_delta(svc)  # a scenario on EQ1/EP1 exists, but a user scoped to S/4 only may not queue a job for it
    s4_only = Principal("s4", "s4", ("data_steward",), attrs={"systems": ["S4Q"]})
    with pytest.raises(Forbidden):
        svc.orch.submit(s4_only, "delta_run", {"scenario_id": sc.id})
    steward = Principal("s", "s", ("data_steward",), attrs={"systems": ["EQ1", "EP1"]})
    with pytest.raises(Forbidden):
        svc.lean.build(steward, {"template_id": "none", "host_id": svc.s4_tgt, "source_id": svc.s4_src, "client": "320"})


def test_scoped_user_cannot_use_agents_to_read_beyond_the_scope(svc):
    p = scoped_project(svc)
    svc.build_plan(CORA, p.id)
    svc.agents.run(CORA, "business-dependency", {"project_id": p.id})  # inside the scope: fine
    other = svc.create_project(ALICE, "other", svc.s4_src, svc.s4_tgt)
    with pytest.raises(Forbidden):
        svc.agents.run(CORA, "business-dependency", {"project_id": other.id})
    for agent in ("landscape-discovery", "compliance-verification", "performance-optimization"):
        with pytest.raises(Forbidden, match="whole platform"):
            svc.agents.run(CORA, agent, {})


def test_scoped_user_over_http_sees_and_touches_only_the_scope(tmp_path):
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    mine = c.post("/api/projects", json={"name": "mine", "source_id": b["source"]["id"], "target_id": b["target"]["id"]}, headers=H("cora.regional"))
    assert mine.status_code == 201
    other = c.post("/api/projects", json={"name": "s4", "source_id": b["s4_source"]["id"], "target_id": b["s4_target"]["id"]}, headers=H("alice.basis")).json()
    assert c.post("/api/projects", json={"name": "x", "source_id": b["s4_source"]["id"], "target_id": b["s4_target"]["id"]}, headers=H("cora.regional")).status_code == 403
    assert {s["sid"] for s in c.get("/api/systems", headers=H("cora.regional")).json()} == {"EP1", "EQ1"}
    assert len(c.get("/api/systems", headers=H("alice.basis")).json()) == 4
    assert [p["name"] for p in c.get("/api/projects", headers=H("cora.regional")).json()] == ["mine"]
    assert len(c.get("/api/projects", headers=H("alice.basis")).json()) == 2
    for path in (f"/api/projects/{other['id']}", f"/api/projects/{other['id']}/plan", f"/api/systems/{b['s4_source']['id']}/discovery",
                 f"/api/postcopy/systems/{b['s4_target']['id']}/assessment", f"/api/orchestration/windows/{b['s4_target']['id']}"):
        assert c.get(path, headers=H("cora.regional")).status_code == 403, path
    assert c.get(f"/api/projects/{mine.json()['id']}", headers=H("cora.regional")).status_code == 200
    assert c.get("/api/projects/nope", headers=H("cora.regional")).status_code == 404  # unknown ids still answer 404
    assert c.get("/api/me", headers=H("cora.regional")).json()["scope"]["company_codes"] == ["2000"]
    assert c.get("/api/tdm/templates/o2c_complete/candidates", params={"source_id": b["s4_source"]["id"]}, headers=H("cora.regional")).status_code == 403
    assert c.get("/api/tdm/templates/o2c_complete/candidates", params={"source_id": b["source"]["id"], "company_code": "1000"}, headers=H("cora.regional")).status_code == 403


# ======================================================================== OIDC
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_of(key, kid, alg="RS256"):
    pub = key.public_key()
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(pub)) if isinstance(key, rsa.RSAPrivateKey) else json.loads(jwt.algorithms.ECAlgorithm.to_jwk(pub))
    return {**d, "kid": kid, "use": "sig", "alg": alg}


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def token(key, kid="k1", alg="RS256", **over):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "u-1", "name": "Una User", "roles": ["data_steward"], "iat": now, "exp": now + 300, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, pem(key), algorithm=alg, headers={"kid": kid})


@pytest.fixture
def idp(tmp_path):
    key = rsa_key()
    jwks = {"keys": [jwk_of(key, "k1")]}
    cfg = AuthConfig(mode="oidc", issuer=ISS, audience=AUD)
    app = create_app(tmp_path, persist=False, auth=cfg, jwks=jwks)
    return type("Idp", (), {"key": key, "jwks": jwks, "app": app, "client": TestClient(app), "cfg": cfg})()


def bearer(t):
    return {"Authorization": f"Bearer {t}"}


def test_valid_token_authenticates_and_maps_claims(idp):
    r = idp.client.get("/api/me", headers=bearer(token(idp.key, roles=["data_steward", "made_up_role"], rf_company_codes=["2000"], rf_systems=["EQ1"])))
    me = r.json()
    assert r.status_code == 200 and me["id"] == "u-1" and me["roles"] == ["data_steward"] and me["auth"] == "oidc"  # unknown role dropped
    assert me["scope"] == {"company_codes": ["2000"], "systems": ["EQ1"]} and "plan:write" in me["permissions"] and "plan:approve" not in me["permissions"]


def test_oidc_mode_ignores_the_demo_header_and_hides_the_demo_directory(idp):
    assert idp.client.get("/api/me", headers={"X-Demo-User": "root.admin"}).status_code == 401
    assert idp.client.get("/api/users").json() == []
    cfg = idp.client.get("/api/auth/config").json()
    assert cfg["mode"] == "oidc" and cfg["demo_header_accepted"] is False
    assert idp.client.get("/api/health").status_code == 200  # health stays open


@pytest.mark.parametrize("bad,reason", [
    (lambda i: token(i.key, exp=int(time.time()) - 3600), "expired"),
    (lambda i: token(i.key, aud="someone-else"), "audience"),
    (lambda i: token(i.key, iss="https://evil.example"), "issuer"),
    (lambda i: token(rsa_key()), "signature"),
    (lambda i: token(i.key, sub=None), "claims"),
    (lambda i: token(i.key, exp=None), "claims"),
    (lambda i: token(i.key, rf_kind="wizard"), "claims"),
    (lambda i: token(i.key, rf_company_codes="2000"), "claims"),
    (lambda i: token(i.key, rf_systems=[1, 2]), "claims"),
    (lambda i: token(i.key, kid="unknown"), "key"),
    (lambda i: "not-a-jwt", "malformed"),
    (lambda i: "a" * 9000, "malformed"),
])
def test_bad_tokens_are_refused_with_a_reason(idp, bad, reason):
    r = idp.client.get("/api/me", headers=bearer(bad(idp)))
    assert r.status_code == 401 and f"({reason})" in r.json()["detail"] and "Bearer" in r.headers["www-authenticate"]


def test_unsigned_and_hmac_forged_tokens_are_refused(idp):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "attacker", "roles": ["admin"], "exp": now + 300}
    unsigned = jwt.encode(claims, key=None, algorithm="none", headers={"kid": "k1"})
    assert idp.client.get("/api/me", headers=bearer(unsigned)).status_code == 401
    public_pem = idp.key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    try:  # the classic confusion attack: HS256 signed with the public key as the secret
        forged = jwt.encode(claims, public_pem, algorithm="HS256", headers={"kid": "k1"})
    except Exception:  # newer PyJWT refuses to build it; craft by hand
        import base64, hashlib, hmac
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
        head, body = b64(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode()), b64(json.dumps(claims).encode())
        forged = (head + b"." + body + b"." + b64(hmac.new(public_pem, head + b"." + body, hashlib.sha256).digest())).decode()
    r = idp.client.get("/api/me", headers=bearer(forged))
    assert r.status_code == 401 and "(alg)" in r.json()["detail"]


def test_missing_credentials_and_wrong_scheme(idp):
    assert idp.client.get("/api/me").status_code == 401
    assert idp.client.get("/api/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_ec_keys_work_and_algorithm_must_match_the_key(tmp_path):
    ec_key = ec.generate_private_key(ec.SECP256R1())
    jwks = {"keys": [jwk_of(ec_key, "e1", "ES256")]}
    app = create_app(tmp_path, persist=False, auth=AuthConfig(mode="oidc", issuer=ISS, audience=AUD), jwks=jwks)
    c = TestClient(app)
    assert c.get("/api/me", headers=bearer(token(ec_key, kid="e1", alg="ES256"))).status_code == 200
    rsa_k = rsa_key()
    assert c.get("/api/me", headers=bearer(token(rsa_k, kid="e1", alg="RS256"))).status_code == 401  # kid points at an ES256 key


def test_agent_tokens_never_gain_agent_forbidden_permissions(idp):
    t = token(idp.key, sub="bot-1", rf_kind="agent", roles=["admin", "approver", "basis"])
    me = idp.client.get("/api/me", headers=bearer(t)).json()
    assert me["kind"] == "agent" and not ({"plan:approve", "run:execute", "system:write"} & set(me["permissions"]))
    assert idp.client.post("/api/orchestration/tick", headers=bearer(t)).status_code == 403
    human = token(idp.key, sub="boss", roles=["basis", "approver"])
    assert idp.client.post("/api/demo/bootstrap", headers=bearer(human)).status_code == 200


def test_roles_come_from_the_token_so_least_privilege_holds(idp):
    viewer = bearer(token(idp.key, sub="v", roles=["viewer"]))
    admin = bearer(token(idp.key, sub="a", roles=["basis", "approver"]))
    idp.client.post("/api/demo/bootstrap", headers=admin)
    b = idp.client.get("/api/systems", headers=viewer).json()
    assert b and idp.client.post("/api/projects", json={"name": "x", "source_id": b[0]["id"], "target_id": b[1]["id"]}, headers=viewer).status_code == 403
    nobody = bearer(token(idp.key, sub="n", roles=[]))
    assert idp.client.get("/api/systems", headers=nobody).status_code == 403  # no roles, no 'view'


def test_abac_attributes_from_the_token_are_enforced(idp):
    admin = bearer(token(idp.key, sub="a", roles=["basis", "approver"]))
    b = idp.client.post("/api/demo/bootstrap", headers=admin).json()
    scoped = bearer(token(idp.key, sub="reg", roles=["data_steward"], rf_systems=["EP1", "EQ1"], rf_company_codes=["2000"]))
    assert {s["sid"] for s in idp.client.get("/api/systems", headers=scoped).json()} == {"EP1", "EQ1"}
    assert idp.client.post("/api/projects", json={"name": "x", "source_id": b["s4_source"]["id"], "target_id": b["s4_target"]["id"]}, headers=scoped).status_code == 403


def test_failed_logins_are_audited_without_the_token_and_the_audit_is_capped(idp):
    secret = token(idp.key, aud="nope")
    for _ in range(30):
        idp.client.get("/api/me", headers=bearer(secret))
    ents = [e for e in idp.app.state.svc.audit.entries() if e["action"] == "auth.failed"]
    assert 1 <= len(ents) <= 20 and ents[0]["details"]["reason"] == "audience"
    assert secret not in json.dumps(idp.app.state.svc.audit.entries())
    assert idp.app.state.svc.audit.verify()["valid"]


def test_key_rotation_reaches_the_platform_without_a_restart(tmp_path):
    old, new = rsa_key(), rsa_key()
    f = tmp_path / "jwks.json"
    f.write_text(json.dumps({"keys": [jwk_of(old, "k1")]}))
    cfg = AuthConfig(mode="oidc", issuer=ISS, audience=AUD, jwks_file=str(f))
    app = create_app(tmp_path / "d", persist=False, auth=cfg)
    c = TestClient(app)
    assert c.get("/api/me", headers=bearer(token(old, "k1"))).status_code == 200
    assert c.get("/api/me", headers=bearer(token(new, "k2"))).status_code == 401  # not published yet
    f.write_text(json.dumps({"keys": [jwk_of(old, "k1"), jwk_of(new, "k2")]}))
    app.state.verifier._loaded = 0  # the forced re-read is rate limited to once a second
    assert c.get("/api/me", headers=bearer(token(new, "k2"))).status_code == 200
    f.write_text(json.dumps({"keys": [jwk_of(new, "k2")]}))  # old key retired
    app.state.verifier._loaded = 0
    assert c.get("/api/me", headers=bearer(token(new, "k2"))).status_code == 200
    app.state.verifier._jwks = None
    assert c.get("/api/me", headers=bearer(token(old, "k1"))).status_code == 401


def test_configuration_is_validated(monkeypatch):
    monkeypatch.setenv("RFACTORY_AUTH", "oidc")
    for k in ("RFACTORY_OIDC_ISSUER", "RFACTORY_OIDC_AUDIENCE", "RFACTORY_OIDC_JWKS_FILE", "RFACTORY_OIDC_JWKS_URL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValueError, match="needs"):
        AuthConfig.from_env()
    monkeypatch.setenv("RFACTORY_AUTH", "magic")
    with pytest.raises(ValueError):
        AuthConfig.from_env()
    monkeypatch.setenv("RFACTORY_AUTH", "demo")
    assert AuthConfig.from_env().mode == "demo"


def test_demo_mode_ignores_bearer_tokens(tmp_path):
    c = TestClient(create_app(tmp_path, persist=False, auth=AuthConfig()))
    assert c.get("/api/me", headers={"Authorization": "Bearer x"}).status_code == 401
    assert c.get("/api/me", headers={"X-Demo-User": "alice.basis"}).json()["auth"] == "demo"
    assert c.get("/api/auth/config").json()["demo_header_accepted"] is True


def test_combinations_and_query_parameters_respect_the_scope(tmp_path):
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    combos = c.get("/api/landscape/combinations", headers=H("cora.regional")).json()
    assert combos and all("S4" not in x["label"] for x in combos)
    assert any("S4" in x["label"] for x in c.get("/api/landscape/combinations", headers=H("alice.basis")).json())
    q = {"source_id": b["s4_source"]["id"], "target_id": b["s4_target"]["id"]}
    assert c.get("/api/full-refresh/plan", params=q, headers=H("cora.regional")).status_code == 403
    assert c.get("/api/full-refresh/plan", params=q, headers=H("alice.basis")).status_code == 200
    assert c.post("/api/demo/simulate-system-copy", params=q, headers=H("cora.regional")).status_code == 403


# ---- browser login configuration (Authorization Code + PKCE is carried out by the browser; the API only advertises it)
def _cfg(**kw):
    return AuthConfig(mode="oidc", issuer=ISS, audience=AUD, jwks_file="x", **kw)


def test_login_configuration_is_advertised_only_when_complete_and_in_oidc_mode(tmp_path):
    full = _cfg(authorize_url="https://idp.example/authorize", token_url="https://idp.example/token", client_id="keystone-ui")
    pub = full.public()
    assert pub["login"] == {"authorize_url": "https://idp.example/authorize", "token_url": "https://idp.example/token", "client_id": "keystone-ui",
                            "scope": "openid profile", "end_session_url": None} and "PKCE" in pub["login_flow"]
    assert "secret" not in json.dumps(pub).lower()  # a public client: there is no secret to leak
    assert _cfg().public()["login"] is None and "paste" in _cfg().public()["login_flow"]
    demo = AuthConfig(mode="demo", authorize_url="https://idp.example/a", token_url="https://idp.example/t", client_id="c")
    assert demo.public()["login"] is None  # the demo header mode never advertises a real login
    c = TestClient(create_app(tmp_path, persist=False, auth=full, jwks={"keys": []}))
    assert c.get("/api/auth/config").json()["login"]["client_id"] == "keystone-ui"


@pytest.mark.parametrize("kw,why", [
    (dict(authorize_url="https://i/a"), "together"),
    (dict(authorize_url="https://i/a", token_url="https://i/t"), "together"),
    (dict(authorize_url="http://idp.example/a", token_url="https://i/t", client_id="c"), "https"),
    (dict(authorize_url="https://i/a", token_url="http://evil.example/t", client_id="c"), "https"),
    (dict(authorize_url="https://i/a", token_url="https://i/t", client_id="c", end_session_url="http://x.example/out"), "https"),
    (dict(authorize_url="https://i/a", token_url="https://i/t", client_id="c", scope="profile"), "openid"),
])
def test_unsafe_or_partial_login_configuration_is_refused(tmp_path, kw, why):
    with pytest.raises(ValueError, match=why):
        create_app(tmp_path, persist=False, auth=_cfg(**kw), jwks={"keys": []})


def test_login_configuration_comes_from_the_environment(monkeypatch):
    for k, v in {"RFACTORY_AUTH": "oidc", "RFACTORY_OIDC_ISSUER": ISS, "RFACTORY_OIDC_AUDIENCE": AUD, "RFACTORY_OIDC_JWKS_FILE": "x",
                 "RFACTORY_OIDC_AUTHORIZE_URL": "http://localhost:9000/authorize", "RFACTORY_OIDC_TOKEN_URL": "http://localhost:9000/token",
                 "RFACTORY_OIDC_CLIENT_ID": "ui", "RFACTORY_OIDC_SCOPE": "openid email"}.items():
        monkeypatch.setenv(k, v)
    c = AuthConfig.from_env()
    assert c.login_configured() and c.scope == "openid email"
    monkeypatch.setenv("RFACTORY_OIDC_CLIENT_ID", "")
    with pytest.raises(ValueError, match="together"):
        AuthConfig.from_env()

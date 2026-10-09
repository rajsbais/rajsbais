import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from sdtf.security import oidc

API = "/api/v1"


@pytest.fixture(scope="module")
def idp(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("oidc") / "jwks.json"
    path.write_text(json.dumps(oidc.jwks_for(key.public_key(), "kid-1")))
    cfg = oidc.OidcConfig(issuer="https://idp.example.com/realms/sdtf", audience="sdtf-api", jwks_url="", jwks_file=str(path), groups_claim="groups", role_map={"SAP-Migration-Architects": "architect", "SAP-Business-Approvers": "approver"})
    oidc.configure(cfg)
    yield {"key": key, "cfg": cfg}
    oidc.configure(oidc.OidcConfig(issuer="", audience="", jwks_url="", jwks_file=""))


def _token(idp, **over):
    claims = {"iss": idp["cfg"].issuer, "aud": "sdtf-api", "sub": "u-1", "preferred_username": "jane.doe", "groups": ["SAP-Migration-Architects"], "exp": int(time.time()) + 600, "iat": int(time.time())}
    claims.update(over)
    return oidc.make_rs256_token(idp["key"], over.pop("kid", "kid-1") if "kid" in over else "kid-1", claims)


def test_valid_token_maps_groups_to_roles(idp):
    p = oidc.principal_from_claims(oidc.verify_jwt(_token(idp)))
    assert p.username == "jane.doe" and p.roles == ["architect"] and p.tenant_id == "default"
    p2 = oidc.principal_from_claims(oidc.verify_jwt(_token(idp, groups=["SAP-Business-Approvers", "unknown-group"], tenant="acme")))
    assert p2.roles == ["approver"] and p2.tenant_id == "acme"
    assert oidc.principal_from_claims(oidc.verify_jwt(_token(idp, groups=[]))).roles == ["viewer"]


@pytest.mark.parametrize("bad", [dict(exp=int(time.time()) - 5), dict(iss="https://evil"), dict(aud="other-api")])
def test_invalid_claims_rejected(idp, bad):
    with pytest.raises(oidc.OidcError):
        oidc.verify_jwt(_token(idp, **bad))


def test_forged_signature_rejected(idp):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = oidc.make_rs256_token(other, "kid-1", {"iss": idp["cfg"].issuer, "aud": "sdtf-api", "sub": "x", "exp": int(time.time()) + 600})
    with pytest.raises(oidc.OidcError, match="signature"):
        oidc.verify_jwt(forged)
    h, p, s = _token(idp).split(".")
    with pytest.raises(oidc.OidcError):
        oidc.verify_jwt(f"{h}.{p}.{s[:-3]}abc")


def test_api_accepts_oidc_bearer_and_enforces_roles(client, idp):
    arch = {"Authorization": f"Bearer {_token(idp)}"}
    r = client.get(f"{API}/auth/me", headers=arch)
    assert r.status_code == 200 and r.json() == {"username": "jane.doe", "roles": ["architect"], "tenant_id": "default"}
    assert client.post(f"{API}/projects", json={"name": "oidc", "scenario_type": "SDT"}, headers=arch).status_code == 201
    viewer = {"Authorization": f"Bearer {_token(idp, groups=[])}"}
    assert client.post(f"{API}/projects", json={"name": "x", "scenario_type": "SDT"}, headers=viewer).status_code == 403
    assert client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {_token(idp, exp=int(time.time()) - 1)}"}).status_code == 401
    # tenant isolation: a principal of tenant acme does not see tenant default projects
    acme = {"Authorization": f"Bearer {_token(idp, tenant='acme')}"}
    assert all(p["name"] != "oidc" for p in client.get(f"{API}/projects", headers=acme).json())


# ----------------------------------------------------------------------------- browser PKCE login (API-side exchange)
class FakeIdp:
    """Minimal authorization server for tests: discovery document + token endpoint enforcing PKCE S256."""

    def __init__(self, key, issuer):
        import hashlib

        self.key, self.issuer = key, issuer
        self.codes: dict[str, dict] = {}  # code -> {challenge, nonce, redirect_uri, used}
        self.refresh_tokens: set[str] = set()
        self.requests: list[dict] = []
        self._sha = hashlib.sha256

    def authorize(self, challenge: str, nonce: str, redirect_uri: str) -> str:
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": challenge, "nonce": nonce, "redirect_uri": redirect_uri, "used": False}
        return code

    def _tokens(self, nonce, groups=("SAP-Migration-Architects",)):
        now = int(time.time())
        self.issued = getattr(self, "issued", 0) + 1
        base = {"iss": self.issuer, "aud": "sdtf-api", "sub": "u-7", "preferred_username": "jane.doe", "name": "Jane Doe", "groups": list(groups), "exp": now + 300, "iat": now, "jti": f"jti-{self.issued}"}
        idt = oidc.make_rs256_token(self.key, "kid-1", {**base, "nonce": nonce})
        acc = oidc.make_rs256_token(self.key, "kid-1", {**base, "typ": "Bearer"})
        rt = f"rt-{len(self.refresh_tokens) + 1}"
        self.refresh_tokens.add(rt)
        return {"access_token": acc, "id_token": idt, "refresh_token": rt, "token_type": "Bearer", "expires_in": 300}

    def handler(self, request):
        from urllib.parse import parse_qs

        import httpx

        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/protocol/openid-connect/auth", "token_endpoint": f"{self.issuer}/protocol/openid-connect/token", "end_session_endpoint": f"{self.issuer}/protocol/openid-connect/logout", "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs"})
        if request.url.path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.requests.append(form)
            if form.get("client_id") != "sdtf-api":
                return httpx.Response(401, json={"error": "invalid_client"})
            if form.get("grant_type") == "refresh_token":
                if form.get("refresh_token") not in self.refresh_tokens:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "unknown refresh token"})
                return httpx.Response(200, json=self._tokens(None))
            c = self.codes.get(form.get("code", ""))
            if c is None or c["used"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "code is invalid or already used"})
            expect = oidc._b64(self._sha(form.get("code_verifier", "").encode()).digest())
            if expect != c["challenge"] or form.get("redirect_uri") != c["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE verification failed"})
            c["used"] = True
            return httpx.Response(200, json=self._tokens(c["nonce"]))
        return httpx.Response(404)


@pytest.fixture
def pkce_idp(idp, monkeypatch):
    import httpx

    fake = FakeIdp(idp["key"], idp["cfg"].issuer)
    cfg = oidc.OidcConfig(**{**idp["cfg"].__dict__, "client_id": "", "exchange": "api"})
    oidc.configure(cfg, transport=httpx.MockTransport(fake.handler))
    yield fake
    oidc.configure(idp["cfg"])


def _pkce_pair():
    import hashlib
    import secrets

    verifier = oidc._b64(secrets.token_bytes(48))
    return verifier, oidc._b64(hashlib.sha256(verifier.encode()).digest())


def test_oidc_public_config_from_discovery(client, pkce_idp):
    cfg = client.get(f"{API}/auth/oidc/config").json()
    assert cfg["enabled"] and cfg["client_id"] == "sdtf-api" and cfg["pkce"] == "S256" and cfg["exchange"] == "api"
    assert cfg["authorization_endpoint"].endswith("/auth") and cfg["token_endpoint"].endswith("/token") and cfg["end_session_endpoint"].endswith("/logout")
    assert cfg["dev_login"] is True and "client_secret" not in cfg


def test_oidc_public_config_when_disabled(client):
    oidc.configure(oidc.OidcConfig(issuer="", audience="", jwks_url="", jwks_file=""))
    try:
        assert client.get(f"{API}/auth/oidc/config").json() == {"enabled": False, "dev_login": True}
    finally:
        pass  # the module fixture restores the enabled config on next use


def test_pkce_exchange_verifies_id_token_and_nonce(client, pkce_idp):
    verifier, challenge = _pkce_pair()
    redirect = "http://localhost:5173/auth/callback"
    code = pkce_idp.authorize(challenge, "nonce-xyz", redirect)
    r = client.post(f"{API}/auth/oidc/exchange", json={"code": code, "code_verifier": verifier, "redirect_uri": redirect, "nonce": "nonce-xyz"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["username"] == "jane.doe" and out["roles"] == ["architect"] and out["display_name"] == "Jane Doe" and out["token_kind"] == "access_token"
    assert out["expires_at"] > time.time() and out["refresh_token"] in pkce_idp.refresh_tokens
    assert pkce_idp.requests[-1]["code_verifier"] == verifier and "client_secret" not in pkce_idp.requests[-1]
    # the returned bearer is accepted by the API with the mapped roles
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {out['access_token']}"})
    assert me.status_code == 200 and me.json()["roles"] == ["architect"]
    # a code cannot be replayed
    assert client.post(f"{API}/auth/oidc/exchange", json={"code": code, "code_verifier": verifier, "redirect_uri": redirect, "nonce": "nonce-xyz"}).status_code == 401
    # login is on the audit trail
    admin = {"Authorization": f"Bearer {client.post(f'{API}/auth/token', json={'username': 'admin', 'password': 'admin'}).json()['access_token']}"}
    evs = client.get(f"{API}/audit/events", headers=admin, params={"limit": 50}).json()
    assert any(e["action"] == "LOGIN" and e["actor"] == "jane.doe" and e["details"].get("method") == "oidc" for e in evs)


def test_pkce_exchange_rejects_wrong_verifier_and_nonce(client, pkce_idp):
    verifier, challenge = _pkce_pair()
    other, _ = _pkce_pair()
    redirect = "http://localhost:5173/auth/callback"
    code = pkce_idp.authorize(challenge, "n1", redirect)
    r = client.post(f"{API}/auth/oidc/exchange", json={"code": code, "code_verifier": other, "redirect_uri": redirect, "nonce": "n1"})
    assert r.status_code == 401 and "PKCE" in r.json()["detail"]
    code = pkce_idp.authorize(challenge, "n1", redirect)
    r = client.post(f"{API}/auth/oidc/exchange", json={"code": code, "code_verifier": verifier, "redirect_uri": redirect, "nonce": "stale"})
    assert r.status_code == 401 and "nonce" in r.json()["detail"]
    assert client.post(f"{API}/auth/oidc/exchange", json={"code": "", "code_verifier": "", "redirect_uri": ""}).status_code == 422


def test_pkce_refresh_and_browser_side_exchange(client, pkce_idp):
    verifier, challenge = _pkce_pair()
    redirect = "http://localhost:5173/auth/callback"
    first = client.post(f"{API}/auth/oidc/exchange", json={"code": pkce_idp.authorize(challenge, "n2", redirect), "code_verifier": verifier, "redirect_uri": redirect, "nonce": "n2"}).json()
    r = client.post(f"{API}/auth/oidc/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200 and r.json()["username"] == "jane.doe" and r.json()["access_token"] != first["access_token"]
    assert client.post(f"{API}/auth/oidc/refresh", json={"refresh_token": "bogus"}).status_code == 401
    # SDTF_OIDC_EXCHANGE=browser: the SPA talked to the token endpoint itself and only asks the API to verify
    tokens = pkce_idp._tokens("n3")
    r = client.post(f"{API}/auth/oidc/exchange", json={"tokens": tokens, "nonce": "n3"})
    assert r.status_code == 200 and r.json()["token_kind"] == "access_token"
    forged = {**tokens, "id_token": tokens["id_token"][:-4] + "AAAA"}
    assert client.post(f"{API}/auth/oidc/exchange", json={"tokens": forged, "nonce": "n3"}).status_code == 401

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

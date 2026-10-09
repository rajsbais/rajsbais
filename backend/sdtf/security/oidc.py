"""OpenID Connect bearer-token verification (enterprise SSO).

The API accepts, in addition to the dev HMAC tokens, RS256 JWTs issued by an OIDC provider. Configuration:
  SDTF_OIDC_ISSUER      expected `iss`
  SDTF_OIDC_AUDIENCE    expected `aud` (client id)
  SDTF_OIDC_JWKS_URL    JWKS endpoint (fetched lazily, cached) - or
  SDTF_OIDC_JWKS_FILE   local JWKS document (air-gapped deployments, tests)
  SDTF_OIDC_ROLE_MAP    JSON {"<group or role claim value>": "<sdtf role>"}; default maps identical names
  SDTF_OIDC_GROUPS_CLAIM claim holding groups/roles (default "groups")
Only signature, issuer, audience and expiry are verified here; the authorization code flow itself happens in the
identity provider / frontend (PKCE), which hands the ID/access token to the API.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..security.auth import ROLES, Principal


class OidcError(Exception):
    pass


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class OidcConfig:
    issuer: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_ISSUER", ""))
    audience: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_AUDIENCE", ""))
    jwks_url: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_JWKS_URL", ""))
    jwks_file: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_JWKS_FILE", ""))
    groups_claim: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_GROUPS_CLAIM", "groups"))
    role_map: dict = field(default_factory=lambda: json.loads(os.getenv("SDTF_OIDC_ROLE_MAP", "{}") or "{}"))
    tenant_claim: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_TENANT_CLAIM", "tenant"))

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and (self.jwks_url or self.jwks_file))


class JwksCache:
    def __init__(self, cfg: OidcConfig, ttl: int = 3600):
        self.cfg = cfg
        self.ttl = ttl
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._loaded_at = 0.0

    def _load(self) -> None:
        if self.cfg.jwks_file:
            with open(self.cfg.jwks_file, encoding="utf-8") as fh:
                doc = json.load(fh)
        elif self.cfg.jwks_url:
            import httpx

            doc = httpx.get(self.cfg.jwks_url, timeout=10).json()
        else:
            raise OidcError("no JWKS source configured")
        keys = {}
        for k in doc.get("keys", []):
            if k.get("kty") != "RSA":
                continue
            n = int.from_bytes(_unb64(k["n"]), "big")
            e = int.from_bytes(_unb64(k["e"]), "big")
            keys[k.get("kid", "")] = rsa.RSAPublicNumbers(e, n).public_key()
        self._keys, self._loaded_at = keys, time.time()

    def key(self, kid: str) -> rsa.RSAPublicKey:
        if not self._keys or time.time() - self._loaded_at > self.ttl or kid not in self._keys:
            self._load()
        if kid not in self._keys:
            raise OidcError(f"unknown key id {kid!r}")
        return self._keys[kid]


_cache: JwksCache | None = None
_config: OidcConfig | None = None


def configure(cfg: OidcConfig | None = None) -> OidcConfig:
    global _cache, _config
    _config = cfg or OidcConfig()
    _cache = JwksCache(_config)
    return _config


def config() -> OidcConfig:
    return _config or configure()


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and token.startswith("eyJ")


def verify_jwt(token: str, cfg: OidcConfig | None = None, now: float | None = None) -> dict:
    cfg = cfg or config()
    if not cfg.enabled:
        raise OidcError("OIDC is not configured")
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_unb64(h64))
        payload = json.loads(_unb64(p64))
        sig = _unb64(s64)
    except Exception as e:  # noqa: BLE001
        raise OidcError("malformed JWT") from e
    if header.get("alg") != "RS256":
        raise OidcError(f"unsupported alg {header.get('alg')}")
    key = (_cache or JwksCache(cfg)).key(header.get("kid", ""))
    try:
        key.verify(sig, f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception as e:  # noqa: BLE001
        raise OidcError("invalid signature") from e
    now = now or time.time()
    if payload.get("iss") != cfg.issuer:
        raise OidcError("issuer mismatch")
    aud = payload.get("aud")
    if cfg.audience and (cfg.audience != aud and not (isinstance(aud, list) and cfg.audience in aud)):
        raise OidcError("audience mismatch")
    if payload.get("exp", 0) < now:
        raise OidcError("token expired")
    if payload.get("nbf", 0) > now + 60:
        raise OidcError("token not yet valid")
    return payload


def principal_from_claims(payload: dict, cfg: OidcConfig | None = None) -> Principal:
    cfg = cfg or config()
    groups = payload.get(cfg.groups_claim) or []
    if isinstance(groups, str):
        groups = [groups]
    roles = []
    for g in groups:
        r = cfg.role_map.get(g, g if g in ROLES else None)
        if r and r in ROLES and r not in roles:
            roles.append(r)
    if not roles:
        roles = ["viewer"]
    return Principal(username=payload.get("preferred_username") or payload.get("email") or payload.get("sub", "unknown"), roles=roles, tenant_id=payload.get(cfg.tenant_claim) or "default", attributes={"sub": payload.get("sub"), "idp": payload.get("iss")})


# ----------------------------------------------------------------------------- test / tooling helpers
def make_rs256_token(private_key: rsa.RSAPrivateKey, kid: str, claims: dict) -> str:
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = private_key.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64(sig)}"


def jwks_for(public_key: rsa.RSAPublicKey, kid: str) -> dict:
    nums = public_key.public_numbers()
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": _b64(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")), "e": _b64(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big"))}]}

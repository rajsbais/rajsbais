"""OpenID Connect bearer-token verification (enterprise SSO).

The API accepts, in addition to the dev HMAC tokens, RS256 JWTs issued by an OIDC provider. Configuration:
  SDTF_OIDC_ISSUER      expected `iss`
  SDTF_OIDC_AUDIENCE    expected `aud` (client id)
  SDTF_OIDC_JWKS_URL    JWKS endpoint (fetched lazily, cached) - or
  SDTF_OIDC_JWKS_FILE   local JWKS document (air-gapped deployments, tests)
  SDTF_OIDC_ROLE_MAP    JSON {"<group or role claim value>": "<sdtf role>"}; default maps identical names
  SDTF_OIDC_GROUPS_CLAIM claim holding groups/roles (default "groups")
Browser login (authorization code + PKCE, RFC 7636) is driven by the SPA; the API publishes the provider
configuration it needs (`GET /auth/oidc/config`) and, by default, performs the code exchange on the SPA's behalf
(`POST /auth/oidc/exchange`) so the token endpoint never has to be CORS-enabled and the ID token is verified
server-side before it is handed back:
  SDTF_OIDC_CLIENT_ID       public client id the SPA uses (default: SDTF_OIDC_AUDIENCE)
  SDTF_OIDC_CLIENT_SECRET   optional; only for confidential clients when the API performs the exchange
  SDTF_OIDC_DISCOVERY_URL   `<issuer>/.well-known/openid-configuration` by default; endpoints below override it
  SDTF_OIDC_AUTHORIZATION_ENDPOINT / SDTF_OIDC_TOKEN_ENDPOINT / SDTF_OIDC_END_SESSION_ENDPOINT
  SDTF_OIDC_SCOPES          default "openid profile email"
  SDTF_OIDC_EXCHANGE        api (default) | browser - who calls the token endpoint
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
    client_id: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_CLIENT_SECRET", ""))
    discovery_url: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_DISCOVERY_URL", ""))
    authorization_endpoint: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_AUTHORIZATION_ENDPOINT", ""))
    token_endpoint: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_TOKEN_ENDPOINT", ""))
    end_session_endpoint: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_END_SESSION_ENDPOINT", ""))
    scopes: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_SCOPES", "openid profile email"))
    exchange: str = field(default_factory=lambda: os.getenv("SDTF_OIDC_EXCHANGE", "api"))  # api | browser

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and (self.jwks_url or self.jwks_file))

    @property
    def effective_client_id(self) -> str:
        return self.client_id or self.audience


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
_metadata: dict | None = None
_transport = None  # httpx transport override (tests plug a fake identity provider in here)


def configure(cfg: OidcConfig | None = None, transport=None) -> OidcConfig:
    global _cache, _config, _metadata, _transport
    _config = cfg or OidcConfig()
    _cache = JwksCache(_config)
    _metadata = None
    _transport = transport
    return _config


def _client():
    import httpx

    return httpx.Client(transport=_transport, timeout=10)


def config() -> OidcConfig:
    return _config or configure()


def provider_metadata(cfg: OidcConfig | None = None) -> dict:
    """Authorization/token/end-session endpoints: explicit settings win, the rest comes from OIDC discovery
    (fetched once per process). Never raises - a provider that is down yields empty endpoints plus an `error`."""
    global _metadata
    cfg = cfg or config()
    meta = {"authorization_endpoint": cfg.authorization_endpoint, "token_endpoint": cfg.token_endpoint, "end_session_endpoint": cfg.end_session_endpoint, "jwks_uri": cfg.jwks_url}
    if cfg.enabled and not (meta["authorization_endpoint"] and meta["token_endpoint"]):
        if _metadata is None:
            url = cfg.discovery_url or cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                with _client() as c:
                    r = c.get(url)
                    r.raise_for_status()
                    _metadata = r.json()
            except Exception as e:  # noqa: BLE001
                return {**meta, "error": f"OIDC discovery failed: {e}"}
        for k in ("authorization_endpoint", "token_endpoint", "end_session_endpoint", "jwks_uri"):
            meta[k] = meta[k] or _metadata.get(k, "")
    return meta


def public_config(cfg: OidcConfig | None = None) -> dict:
    """What the SPA needs to start a PKCE login. Contains no secrets."""
    cfg = cfg or config()
    if not cfg.enabled:
        return {"enabled": False}
    meta = provider_metadata(cfg)
    return {"enabled": True, "issuer": cfg.issuer, "client_id": cfg.effective_client_id, "scopes": cfg.scopes, "exchange": cfg.exchange if cfg.exchange in ("api", "browser") else "api", "authorization_endpoint": meta["authorization_endpoint"], "token_endpoint": meta["token_endpoint"], "end_session_endpoint": meta["end_session_endpoint"], "pkce": "S256", **({"error": meta["error"]} if meta.get("error") else {})}


def _unverified_claims(token: str) -> dict:
    try:
        return json.loads(_unb64(token.split(".")[1]))
    except Exception:  # noqa: BLE001
        return {}


def _select_bearer(tokens: dict, cfg: OidcConfig, nonce: str | None) -> tuple[str, str, dict]:
    """Pick the token the SPA should present to this API: the access token when it verifies against our
    issuer/audience, else the ID token (whose `aud` is the client id). Returns (token, kind, claims)."""
    id_token = tokens.get("id_token") or ""
    if not id_token:
        raise OidcError("token response carries no id_token")
    id_claims = verify_jwt(id_token, cfg)
    if nonce and id_claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch")
    access = tokens.get("access_token") or ""
    if access and looks_like_jwt(access):
        try:
            return access, "access_token", verify_jwt(access, cfg)
        except OidcError:
            pass
    return id_token, "id_token", id_claims


def _token_request(data: dict, cfg: OidcConfig) -> dict:
    meta = provider_metadata(cfg)
    if not meta.get("token_endpoint"):
        raise OidcError(meta.get("error") or "token endpoint unknown")
    data = {**data, "client_id": cfg.effective_client_id}
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    try:
        with _client() as c:
            r = c.post(meta["token_endpoint"], data=data, headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001
        raise OidcError(f"token endpoint unreachable: {e}") from e
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    if r.status_code != 200:
        raise OidcError(f"token endpoint rejected the request: {body.get('error_description') or body.get('error') or r.status_code}")
    return body


def _login_result(tokens: dict, cfg: OidcConfig, nonce: str | None) -> dict:
    bearer, kind, claims = _select_bearer(tokens, cfg, nonce)
    p = principal_from_claims(claims, cfg)
    id_claims = _unverified_claims(tokens.get("id_token", ""))
    return {
        "access_token": bearer,
        "token_kind": kind,
        "token_type": "bearer",
        "expires_at": int(claims.get("exp", 0)),
        "id_token": tokens.get("id_token"),
        "refresh_token": tokens.get("refresh_token"),
        "username": p.username,
        "roles": p.roles,
        "tenant_id": p.tenant_id,
        "display_name": id_claims.get("name") or id_claims.get("preferred_username") or p.username,
    }


def exchange_code(code: str, code_verifier: str, redirect_uri: str, nonce: str | None = None, cfg: OidcConfig | None = None) -> dict:
    """Authorization-code grant with PKCE on behalf of the SPA. The code verifier is forwarded untouched; the
    returned ID token is verified (signature, issuer, audience, expiry, nonce) before anything is handed back."""
    cfg = cfg or config()
    if not cfg.enabled:
        raise OidcError("OIDC is not configured")
    if cfg.exchange == "browser":
        raise OidcError("the browser performs the code exchange in this deployment (SDTF_OIDC_EXCHANGE=browser)")
    tokens = _token_request({"grant_type": "authorization_code", "code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri}, cfg)
    return _login_result(tokens, cfg, nonce)


def refresh_tokens(refresh_token: str, cfg: OidcConfig | None = None) -> dict:
    cfg = cfg or config()
    if not cfg.enabled:
        raise OidcError("OIDC is not configured")
    tokens = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, cfg)
    return _login_result(tokens, cfg, None)


def verify_browser_tokens(tokens: dict, nonce: str | None = None, cfg: OidcConfig | None = None) -> dict:
    """SDTF_OIDC_EXCHANGE=browser: the SPA exchanged the code itself and asks the API which token to use."""
    cfg = cfg or config()
    if not cfg.enabled:
        raise OidcError("OIDC is not configured")
    return _login_result(tokens, cfg, nonce)


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

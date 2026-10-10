"""Bearer-token authentication (OIDC-style JWTs) for the API.

What is verified: signature (RS256 or ES256 only; `none` and HMAC algorithms are refused so a token can never be forged with a public key),
`iss`, `aud`, `exp` (and `nbf` when present) with a small clock leeway, and a non-empty `sub`. Keys come from a JWKS (file or URL) and are
re-read when an unknown `kid` appears, which is how a key rotation reaches the platform.

Claims -> principal: roles from the role claim (unknown roles are ignored: least privilege), `rf_kind` (human | agent | service; anything else is
refused), and the ABAC attributes `rf_systems` / `rf_company_codes` / `rf_plants` / `rf_sales_orgs` (lists of strings; see authz.py). An agent token never gets the permissions
agents are denied, whatever roles it carries.

Browser login: the platform advertises an Authorization Code + PKCE (S256) login through /api/auth/config when the authorize URL, token URL and
client id are configured. The flow itself runs in the browser as a PUBLIC client (no client secret exists anywhere in the platform); the API only
ever sees the resulting bearer token. A pasted token still works.

Revocation (security/revocation.py): a token can be revoked by jti and a subject by cutoff, kept across restarts. NOT provided: refresh tokens or silent
renewal (a session ends when the access token expires) and introspection against the identity provider (tokens are trusted until they expire
unless revoked here, so keep them short-lived). Fetching a JWKS from a URL and the browser flow itself have not been exercised against a
real identity provider; the flow is tested against a fake one.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import jwt

from .auth import PERMISSIONS, Principal

ALLOWED_ALGS = ("RS256", "ES256")
KINDS = ("human", "agent", "service")
MAX_TOKEN_BYTES = 8192


class AuthError(Exception):
    """Authentication failed. `reason` is a short machine-readable code that is safe to show to the caller."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason, self.detail = reason, detail


@dataclass
class AuthConfig:
    mode: str = "demo"  # demo | oidc
    issuer: str = ""
    audience: str = ""
    jwks_file: str = ""
    jwks_url: str = ""
    role_claim: str = "roles"
    leeway_s: int = 30
    jwks_ttl_s: int = 600
    # browser login (Authorization Code + PKCE, public client): all three or none
    authorize_url: str = ""
    token_url: str = ""
    client_id: str = ""
    scope: str = "openid profile"
    end_session_url: str = ""
    require_jti: bool = False  # refuse tokens without a jti: every accepted token can then be revoked individually

    def login_configured(self) -> bool:
        return bool(self.authorize_url and self.token_url and self.client_id)

    @classmethod
    def from_env(cls) -> "AuthConfig":
        e = os.environ.get
        mode = e("RFACTORY_AUTH", "demo").lower()
        if mode not in ("demo", "oidc"):
            raise ValueError("RFACTORY_AUTH must be 'demo' or 'oidc'")
        c = cls(mode, e("RFACTORY_OIDC_ISSUER", ""), e("RFACTORY_OIDC_AUDIENCE", ""), e("RFACTORY_OIDC_JWKS_FILE", ""), e("RFACTORY_OIDC_JWKS_URL", ""),
                e("RFACTORY_OIDC_ROLE_CLAIM", "roles"))
        c.authorize_url, c.token_url, c.client_id = e("RFACTORY_OIDC_AUTHORIZE_URL", ""), e("RFACTORY_OIDC_TOKEN_URL", ""), e("RFACTORY_OIDC_CLIENT_ID", "")
        c.scope, c.end_session_url = e("RFACTORY_OIDC_SCOPE", "openid profile"), e("RFACTORY_OIDC_END_SESSION_URL", "")
        c.require_jti = e("RFACTORY_OIDC_REQUIRE_JTI", "") in ("1", "true", "yes")
        if mode == "oidc" and not (c.issuer and c.audience and (c.jwks_file or c.jwks_url)):
            raise ValueError("oidc mode needs RFACTORY_OIDC_ISSUER, RFACTORY_OIDC_AUDIENCE and RFACTORY_OIDC_JWKS_FILE or _URL")
        c.check_login()
        return c

    def check_login(self) -> None:
        given = [bool(self.authorize_url), bool(self.token_url), bool(self.client_id)]
        if any(given) and not all(given):
            raise ValueError("browser login needs RFACTORY_OIDC_AUTHORIZE_URL, RFACTORY_OIDC_TOKEN_URL and RFACTORY_OIDC_CLIENT_ID together")
        for name, u in (("authorize", self.authorize_url), ("token", self.token_url), ("end-session", self.end_session_url)):
            if u and not (u.startswith("https://") or u.startswith(("http://localhost", "http://127.0.0.1"))):
                raise ValueError(f"the {name} URL must be https (plain http only for localhost): the authorization code and tokens travel over it")
        if self.scope and "openid" not in self.scope.split():
            raise ValueError("the login scope must include 'openid'")

    def public(self) -> dict:
        login = self.mode == "oidc" and self.login_configured()
        return {"mode": self.mode, "demo_header_accepted": self.mode == "demo", "issuer": self.issuer or None, "audience": self.audience or None,
                "login_flow": ("authorization code + PKCE (S256), public client" if login else "none: paste a bearer token") if self.mode == "oidc" else "demo user switcher (no real authentication)",
                "login": {"authorize_url": self.authorize_url, "token_url": self.token_url, "client_id": self.client_id, "scope": self.scope,
                          "end_session_url": self.end_session_url or None} if login else None}


class OidcVerifier:
    def __init__(self, cfg: AuthConfig, jwks: dict | None = None):
        self.cfg = cfg
        self.revocations = None  # a RevocationList, attached by the application
        self._jwks = jwks
        self._loaded = 0.0
        self._lock = threading.Lock()

    def _load(self, force: bool = False) -> dict:
        with self._lock:
            fresh = time.time() - self._loaded < (1 if force else self.cfg.jwks_ttl_s)  # a forced re-read (unknown kid) is rate-limited to 1/s
            if self._jwks is not None and (fresh or (not self.cfg.jwks_file and not self.cfg.jwks_url)):
                return self._jwks
            if self.cfg.jwks_file:
                self._jwks = json.loads(Path(self.cfg.jwks_file).read_text())
            elif self.cfg.jwks_url:
                with urllib.request.urlopen(self.cfg.jwks_url, timeout=5) as r:  # noqa: S310 - operator-configured URL
                    self._jwks = json.loads(r.read())
            self._loaded = time.time()
            return self._jwks or {"keys": []}

    def _key(self, kid: str | None, alg: str):
        for attempt in (False, True):  # the second pass re-reads the JWKS: a rotated-in key becomes usable without a restart
            jwk = next((k for k in self._load(force=attempt).get("keys", []) if kid and k.get("kid") == kid and k.get("use", "sig") == "sig"), None)
            if jwk:
                if jwk.get("alg", alg) != alg:
                    raise AuthError("alg", "token algorithm does not match the key")
                return jwt.PyJWK(jwk).key
        raise AuthError("key", "no signing key for this token")

    def verify(self, token: str) -> Principal:
        return self.principal(self.verify_claims(token))

    def verify_claims(self, token: str) -> dict:
        if len(token.encode()) > MAX_TOKEN_BYTES:
            raise AuthError("malformed", "token too large")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("malformed", "not a JWT")
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise AuthError("alg", f"algorithm {alg!r} is not accepted")
        key = self._key(header.get("kid"), alg)
        try:
            claims = jwt.decode(token, key, algorithms=[alg], audience=self.cfg.audience, issuer=self.cfg.issuer, leeway=self.cfg.leeway_s,
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError:
            raise AuthError("expired", "token expired")
        except jwt.InvalidAudienceError:
            raise AuthError("audience", "wrong audience")
        except jwt.InvalidIssuerError:
            raise AuthError("issuer", "wrong issuer")
        except jwt.InvalidSignatureError:
            raise AuthError("signature", "bad signature")
        except jwt.PyJWTError as e:
            raise AuthError("claims", str(e)[:120])
        if self.cfg.require_jti and not claims.get("jti"):
            raise AuthError("claims", "the token has no jti (required: it could not be revoked)")
        if self.revocations is not None:
            self.revocations.check(claims)
        return claims

    def principal(self, claims: dict) -> Principal:
        sub = str(claims.get("sub", "")).strip()
        if not sub:
            raise AuthError("claims", "empty subject")
        raw = claims.get(self.cfg.role_claim, [])
        roles = raw.split() if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
        roles = tuple(r for r in roles if isinstance(r, str) and r in PERMISSIONS)  # unknown roles are dropped, never invented
        kind = claims.get("rf_kind", "human")
        if kind not in KINDS:
            raise AuthError("claims", "unknown principal kind")
        attrs: dict = {}
        for claim, attr in (("rf_systems", "systems"), ("rf_company_codes", "company_codes"), ("rf_plants", "plants"), ("rf_sales_orgs", "sales_orgs")):
            if claim in claims:
                v = claims[claim]
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise AuthError("claims", f"{claim} must be a list of strings")
                attrs[attr] = list(v)
        return Principal(sub, str(claims.get("name") or sub), roles, kind, attrs)

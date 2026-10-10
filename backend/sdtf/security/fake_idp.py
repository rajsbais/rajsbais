"""A deliberately small OpenID Connect provider for local demos and end-to-end tests of the PKCE login.

It is NOT an identity product: anyone can sign in as anyone, and the signing key is generated per process.
It exists so the complete browser flow (discovery -> authorization request -> consent page -> redirect with
code -> PKCE-verified code exchange -> RS256 ID/access tokens -> RP-initiated logout) can be exercised without
a Keycloak/Entra/Okta tenant. Run it with:

    python -m sdtf.cli fake-idp --port 9400

and point the API at it:

    SDTF_OIDC_ISSUER=http://127.0.0.1:9400 SDTF_OIDC_JWKS_URL=http://127.0.0.1:9400/certs \\
    SDTF_OIDC_AUDIENCE=sdtf-ui SDTF_OIDC_ROLE_MAP='{"SAP-Migration-Architects":"architect"}' python -m sdtf.cli serve

The consent page lists the SDTF roles as directory groups; `&auto=<user>` on the authorization request skips
the page (used by the e2e test).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlencode

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import ROLES
from .oidc import _b64, jwks_for, make_rs256_token

GROUPS = {"admin": "SAP-Platform-Admins", "architect": "SAP-Migration-Architects", "approver": "SAP-Business-Approvers", "operator": "SAP-Migration-Operators", "auditor": "SAP-Internal-Audit", "viewer": "SAP-Readers"}
PAGE = """<!doctype html><html><head><title>Fake IdP</title><style>body{font-family:system-ui;max-width:420px;margin:80px auto;color:#222}label{display:block;margin:10px 0}</style></head>
<body><h2>Fake identity provider</h2><p>Test-only OpenID Connect provider. Sign in as any name; pick the directory groups to assert.</p>
<form method="post" action="/consent">{hidden}
<label>Username <input name="username" value="jane.doe"></label><label>Display name <input name="name" value="Jane Doe"></label>
{groups}<label>Tenant <input name="tenant" value="default"></label><button type="submit">Sign in</button></form></body></html>"""


def create_fake_idp(issuer: str) -> FastAPI:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "fake-idp-" + secrets.token_hex(4)
    app = FastAPI(title="SDTF fake OIDC provider (test only)")
    state = {"codes": {}, "refresh": {}, "requests": [], "logouts": 0}
    app.state.fake = state

    @app.get("/.well-known/openid-configuration")
    def discovery():
        return {"issuer": issuer, "authorization_endpoint": f"{issuer}/auth", "token_endpoint": f"{issuer}/token", "end_session_endpoint": f"{issuer}/logout", "jwks_uri": f"{issuer}/certs", "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"], "grant_types_supported": ["authorization_code", "refresh_token"], "id_token_signing_alg_values_supported": ["RS256"]}

    @app.get("/certs")
    def certs():
        return jwks_for(key.public_key(), kid)

    def _issue(sub: str, name: str, groups: list[str], tenant: str, aud: str, nonce: str | None) -> dict:
        now = int(time.time())
        base = {"iss": issuer, "aud": aud, "sub": sub, "preferred_username": sub, "name": name, "email": f"{sub}@example.com", "groups": groups, "tenant": tenant, "iat": now, "exp": now + 300, "jti": secrets.token_hex(8)}
        tokens = {"token_type": "Bearer", "expires_in": 300, "id_token": make_rs256_token(key, kid, {**base, **({"nonce": nonce} if nonce else {})}), "access_token": make_rs256_token(key, kid, {**base, "typ": "Bearer", "scope": "openid profile email"}), "refresh_token": secrets.token_urlsafe(24)}
        state["refresh"][tokens["refresh_token"]] = {"sub": sub, "name": name, "groups": groups, "tenant": tenant, "aud": aud}
        return tokens

    @app.get("/auth")
    def authorize(request: Request):
        q = dict(request.query_params)
        missing = [k for k in ("client_id", "redirect_uri", "state", "code_challenge") if not q.get(k)]
        if missing or q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request", "missing": missing}, status_code=400)
        state["requests"].append(q)
        if q.get("auto"):  # e2e: sign in without the page
            return _grant(q, q["auto"], q["auto"].title(), [GROUPS.get(q.get("role", "architect"), GROUPS["architect"])], q.get("tenant", "default"))
        hidden = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in q.items() if k in ("client_id", "redirect_uri", "state", "nonce", "code_challenge", "scope"))
        groups = "".join(f'<label><input type="checkbox" name="groups" value="{g}" {"checked" if r == "architect" else ""}> {g} → {r}</label>' for r, g in GROUPS.items() if r in ROLES)
        return HTMLResponse(PAGE.replace("{hidden}", hidden).replace("{groups}", groups))

    def _grant(q: dict, username: str, name: str, groups: list[str], tenant: str):
        code = secrets.token_urlsafe(24)
        state["codes"][code] = {"challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "client_id": q["client_id"], "nonce": q.get("nonce"), "sub": username, "name": name, "groups": groups, "tenant": tenant, "used": False, "exp": time.time() + 120}
        return RedirectResponse(q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]}), status_code=302)

    async def _form(request: Request) -> dict[str, list[str]]:  # no python-multipart dependency needed
        return parse_qs((await request.body()).decode())

    @app.post("/consent")
    async def consent(request: Request):
        form = await _form(request)
        one = {k: v[0] for k, v in form.items()}
        q = {k: one.get(k, "") for k in ("client_id", "redirect_uri", "state", "nonce", "code_challenge")}
        return _grant(q, one.get("username") or "jane.doe", one.get("name") or "", form.get("groups", []), one.get("tenant") or "default")

    @app.post("/token")
    async def token(request: Request):
        form = {k: v[0] for k, v in (await _form(request)).items()}
        grant_type, client_id, code = form.get("grant_type", ""), form.get("client_id", ""), form.get("code", "")
        code_verifier, redirect_uri, refresh_token = form.get("code_verifier", ""), form.get("redirect_uri", ""), form.get("refresh_token", "")
        if grant_type == "refresh_token":
            r = state["refresh"].pop(refresh_token, None)
            if r is None:
                return JSONResponse({"error": "invalid_grant", "error_description": "unknown or used refresh token"}, status_code=400)
            return _issue(r["sub"], r["name"], r["groups"], r["tenant"], r["aud"], None)
        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        c = state["codes"].get(code)
        if c is None or c["used"] or c["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant", "error_description": "code invalid, expired or already used"}, status_code=400)
        if c["client_id"] != client_id or c["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant", "error_description": "client_id/redirect_uri mismatch"}, status_code=400)
        if _b64(hashlib.sha256(code_verifier.encode()).digest()) != c["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        c["used"] = True
        return _issue(c["sub"], c["name"], c["groups"], c["tenant"], client_id, c["nonce"])

    @app.get("/logout")
    def logout(post_logout_redirect_uri: str = "/", id_token_hint: str = ""):
        state["logouts"] += 1
        return RedirectResponse(post_logout_redirect_uri, status_code=302)

    @app.get("/")
    def index():
        return {"service": "SDTF fake OIDC provider", "issuer": issuer, "warning": "test only - no real authentication", "logins": len(state["codes"]), "logouts": state["logouts"]}

    return app


def main(argv=None) -> int:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="test-only OIDC provider")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9400)
    ap.add_argument("--issuer", default=None, help="defaults to http://<host>:<port>")
    a = ap.parse_args(argv)
    issuer = a.issuer or f"http://{a.host}:{a.port}"
    uvicorn.run(create_fake_idp(issuer), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

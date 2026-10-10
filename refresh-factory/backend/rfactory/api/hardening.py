"""HTTP hardening and the production switch.

Always on: security headers (a Content-Security-Policy that allows only this origin, plus the identity provider's origin for the browser login's token
request), no framing, no sniffing, no referrer, no device permissions, `Cache-Control: no-store` on API answers, and a request body limit.
HSTS only when asked (RFACTORY_HSTS=1), because it is only correct behind HTTPS.

`RFACTORY_ENV=production` refuses to start unless the deployment is actually production-shaped, and switches the demo endpoints off:
  * authentication must be oidc (no demo header);
  * state must be durable (RFACTORY_DATA_DIR) and keyed from the environment (RFACTORY_STATE_KEY, RFACTORY_AUDIT_KEY), not from key files next to the data;
  * the fake-endpoint test hook must be off.
This is a guard against a misconfigured start, not a security review of the deployment: TLS termination, network placement, secrets management and
the SAP-side configuration are the operator's.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

MAX_BODY_BYTES = int(os.environ.get("RFACTORY_MAX_BODY_BYTES", 5 * 1024 * 1024))


def origin_of(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else ""


def csp(auth) -> str:
    connect = ["'self'"]
    if getattr(auth, "mode", "") == "oidc" and auth.login_configured():
        connect.append(origin_of(auth.token_url))
    # style-src needs 'unsafe-inline' because the UI sets style attributes; scripts are never inline
    return "; ".join(["default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'", "img-src 'self' data:", f"connect-src {' '.join(connect)}",
                      "frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'", "object-src 'none'"])


def headers(auth, hsts: bool) -> dict[str, str]:
    h = {"Content-Security-Policy": csp(auth), "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
         "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()", "Cross-Origin-Opener-Policy": "same-origin",
         "Cross-Origin-Resource-Policy": "same-origin"}
    if hsts:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h


def production_problems(auth, persist: bool, env=None) -> list[str]:
    e = os.environ if env is None else env
    out = []
    if getattr(auth, "mode", "demo") != "oidc":
        out.append("authentication is the demo header: set RFACTORY_AUTH=oidc (and the issuer, audience and JWKS)")
    if not persist:
        out.append("state is not durable: set RFACTORY_DATA_DIR (one instance) or RFACTORY_DATABASE_URL (PostgreSQL, several instances)")
    if not e.get("RFACTORY_STATE_KEY"):
        out.append("RFACTORY_STATE_KEY is not set: the state encryption key would sit in a file next to the data")
    if not e.get("RFACTORY_AUDIT_KEY"):
        out.append("RFACTORY_AUDIT_KEY is not set: the audit signing key would sit in a file next to the data")
    if e.get("RFACTORY_ALLOW_FAKE_ENDPOINTS") == "1":
        out.append("RFACTORY_ALLOW_FAKE_ENDPOINTS=1 is a test hook and must be off")
    return out

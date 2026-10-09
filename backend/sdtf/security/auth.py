"""Authentication and authorisation foundation.

Dev build: HMAC-signed bearer tokens issued for seeded users (no SSO). The `Principal` abstraction and the
role/attribute checks are what an OIDC/SAML integration plugs into (planned, see docs/08-security-approval-model.md).
RBAC roles: admin, architect, approver, operator, auditor, viewer. ABAC: tenant and project membership.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import User

ROLES = ("admin", "architect", "approver", "operator", "auditor", "viewer")
PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "architect": {"project:read", "project:write", "scope:write", "rules:write", "run:start", "agent:run", "data:unmasked", "records:read"},
    "approver": {"project:read", "approve:manifest", "approve:rules", "approve:run", "agent:decide", "records:read", "data:unmasked"},
    "operator": {"project:read", "run:start", "run:resume", "records:read"},
    "auditor": {"project:read", "audit:read", "records:read"},
    "viewer": {"project:read"},
}

DEV_USERS = [
    ("admin", "Platform Admin", "admin", ["admin"]),
    ("architect", "Transformation Architect", "architect", ["architect"]),
    ("approver", "Business Approver", "approver", ["approver"]),
    ("operator", "Migration Operator", "operator", ["operator"]),
    ("auditor", "Compliance Auditor", "auditor", ["auditor"]),
    ("viewer", "Read-only Viewer", "viewer", ["viewer"]),
]


@dataclass
class Principal:
    username: str
    roles: list[str]
    tenant_id: str = "default"
    attributes: dict = field(default_factory=dict)

    def has(self, permission: str) -> bool:
        for r in self.roles:
            perms = PERMISSIONS.get(r, set())
            if "*" in perms or permission in perms:
                return True
        return False


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 50_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, digest = stored.partition("$")
    return hmac.compare_digest(hash_password(password, salt), stored)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user: User) -> str:
    payload = {"sub": user.username, "roles": user.roles, "tenant": user.tenant_id, "exp": int(time.time()) + settings.token_ttl_seconds}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> Principal:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token") from None
    expected = _b64(hmac.new(settings.auth_secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token signature")
    payload = json.loads(_unb64(body))
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    return Principal(username=payload["sub"], roles=payload.get("roles", []), tenant_id=payload.get("tenant", "default"))


def seed_dev_users(session: Session) -> int:
    if not settings.dev_users_enabled:
        return 0
    from sqlalchemy.exc import IntegrityError

    n = 0
    for username, display, pw, roles in DEV_USERS:
        if session.execute(select(User).where(User.username == username)).scalars().first() is not None:
            continue
        try:
            with session.begin_nested():  # several processes may seed at once: a concurrent insert is not an error
                session.add(User(username=username, display_name=display, password_hash=hash_password(pw), roles=roles))
                session.flush()
            n += 1
        except IntegrityError:
            pass
    session.flush()
    return n


def authenticate(session: Session, username: str, password: str) -> User | None:
    u = session.execute(select(User).where(User.username == username, User.active.is_(True))).scalars().first()
    if u and verify_password(password, u.password_hash):
        return u
    return None


def current_principal(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    from . import oidc

    if oidc.looks_like_jwt(token) and oidc.config().enabled:
        try:
            return oidc.principal_from_claims(oidc.verify_jwt(token))
        except oidc.OidcError as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"OIDC token rejected: {e}") from None
    return decode_token(token)


def require(permission: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"permission '{permission}' required")
        return p

    return dep


def assert_project_access(session: Session, principal: Principal, project_id: str):
    from ..models import Project

    proj = session.get(Project, project_id)
    if proj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if proj.tenant_id != principal.tenant_id and not principal.has("*"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project belongs to another tenant")
    return proj


MASKED_FIELDS = {"NAME1", "TXT50", "KTEXT", "BUTXT", "MAKTX"}


def mask_payload(payload: dict, principal: Principal) -> dict:
    """Pseudonymise person/organisation names for principals without the data:unmasked permission."""
    if principal.has("data:unmasked"):
        return payload
    out = dict(payload)
    for f in MASKED_FIELDS:
        if f in out and out[f]:
            out[f] = "tok_" + hashlib.sha256(str(out[f]).encode()).hexdigest()[:10]
    return out


__all__ = ["Principal", "require", "current_principal", "issue_token", "seed_dev_users", "authenticate", "assert_project_access", "mask_payload", "get_db", "ROLES"]

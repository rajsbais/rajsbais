"""Principals, RBAC permissions and separation-of-duties rules.

MVP authentication is a *demo header* (`X-Demo-User`) against a static user directory. Enterprise SSO
(OIDC/SAML), ABAC attributes and tenant isolation are designed (docs/02-architecture.md) but NOT implemented.
"""
from __future__ import annotations

from dataclasses import dataclass

PERMISSIONS: dict[str, set[str]] = {
    "admin": {"system:write", "project:write", "plan:write", "masking:write", "audit:read", "view"},
    "basis": {"system:write", "project:write", "plan:write", "run:execute", "view"},
    "data_steward": {"project:write", "plan:write", "masking:write", "plan:submit", "run:execute", "view"},
    "approver": {"plan:approve", "exception:approve", "view", "audit:read"},
    "privacy_officer": {"masking:write", "masking:reidentify", "view", "audit:read"},
    "auditor": {"audit:read", "view"},
    "viewer": {"view"},
}
# Permissions that an AI agent principal may never hold, whatever roles it is given.
AGENT_FORBIDDEN = {"plan:approve", "exception:approve", "run:execute", "masking:reidentify", "system:write"}


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    roles: tuple[str, ...]
    kind: str = "human"  # human | agent

    def permissions(self) -> set[str]:
        p: set[str] = set()
        for r in self.roles:
            p |= PERMISSIONS.get(r, set())
        if self.kind == "agent":
            p -= AGENT_FORBIDDEN
        return p

    def can(self, perm: str) -> bool:
        return perm in self.permissions()


class Forbidden(PermissionError):
    pass


DEMO_USERS = {
    "alice.basis": Principal("alice.basis", "Alice (Basis)", ("basis", "data_steward")),
    "bob.steward": Principal("bob.steward", "Bob (Data steward)", ("data_steward",)),
    "carol.approver": Principal("carol.approver", "Carol (Change approver)", ("approver",)),
    "dave.privacy": Principal("dave.privacy", "Dave (Privacy officer)", ("privacy_officer",)),
    "erin.auditor": Principal("erin.auditor", "Erin (Auditor)", ("auditor",)),
    "root.admin": Principal("root.admin", "Admin", ("admin", "basis", "approver")),
    "refresh.copilot": Principal("refresh.copilot", "AI Refresh Copilot", ("admin", "approver", "basis"), kind="agent"),
}


def check_separation_of_duties(creator: str, approver: Principal) -> None:
    if approver.id == creator:
        raise Forbidden("separation of duties: the plan creator cannot approve their own plan")
    if approver.kind == "agent":
        raise Forbidden("AI agents may not approve destructive SAP operations")

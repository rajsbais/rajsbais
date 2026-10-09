"""Connection profiles for remote SAP systems. Secrets are references ("env:NAME" or "file:/path"), resolved at connect time, never stored."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("rfc", "odata")


class ProfileError(ValueError):
    pass


def resolve_secret(ref: str) -> str:
    if ref.startswith("env:"):
        v = os.environ.get(ref[4:])
        if v is None:
            raise ProfileError(f"environment variable {ref[4:]} is not set")
        return v
    if ref.startswith("file:"):
        p = Path(ref[5:])
        if not p.exists():
            raise ProfileError(f"secret file {p} does not exist")
        return p.read_text().strip()
    raise ProfileError("secrets must be given as env:NAME or file:/path (plain-text secrets are refused)")


@dataclass
class ConnectionProfile:
    name: str
    kind: str  # rfc | odata
    user: str = ""
    password_ref: str = ""
    client: str = ""
    # rfc
    ashost: str = ""
    sysnr: str = "00"
    # odata
    base_url: str = ""
    # behaviour
    max_scan_rows: int = 200_000  # protects a production system from accidental full-table scans
    page_rows: int = 5_000
    calls_per_minute: int = 600
    timeout_s: float = 60.0
    options: dict = field(default_factory=dict)

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ProfileError(f"kind must be one of {KINDS}")
        if self.kind == "rfc" and not (self.ashost and self.client):
            raise ProfileError("an rfc profile needs ashost and client")
        if self.kind == "odata" and not self.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ProfileError("an odata profile needs an https base_url (plain http only for localhost)")
        if self.password_ref and not self.password_ref.startswith(("env:", "file:")):
            raise ProfileError("password_ref must be env:NAME or file:/path")
        cd = self.options.get("change_documents")
        if cd:
            lim = {"lag_seconds": (0, 86_400), "overlap_seconds": (0, 86_400), "retention_days": (1, 3650)}
            if cd is not True and (not isinstance(cd, dict) or any(k not in lim or not isinstance(v, int) or isinstance(v, bool) or not lim[k][0] <= v <= lim[k][1] for k, v in cd.items())):
                raise ProfileError(f"options.change_documents must be true or an object with integer {', '.join(f'{k} {a}..{b}' for k, (a, b) in lim.items())}")
        if not 1 <= self.page_rows <= 100_000 or self.max_scan_rows < 1:
            raise ProfileError("page_rows must be 1..100000 and max_scan_rows positive")

    def public(self) -> dict:
        return {"name": self.name, "kind": self.kind, "user": self.user, "client": self.client, "ashost": self.ashost, "sysnr": self.sysnr,
                "base_url": self.base_url, "password_ref": self.password_ref.split(":")[0] + ":***" if self.password_ref else "",
                "max_scan_rows": self.max_scan_rows, "page_rows": self.page_rows, "calls_per_minute": self.calls_per_minute,
                "change_documents": self.options.get("change_documents") or False}

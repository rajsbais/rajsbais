"""Tamper-evident audit log: every entry commits to the previous entry's hash.

This gives *tamper evidence* (verify() detects edits/removals/reordering of entries). It is not WORM storage:
a production deployment must also ship entries to immutable storage (object-lock bucket / external SIEM).
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path | None = None):
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._path = path
        if path and path.exists():
            self._entries = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    @staticmethod
    def _digest(e: dict) -> str:
        body = {k: e[k] for k in ("seq", "ts", "actor", "action", "resource", "details", "prev")}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    def append(self, actor: str, action: str, resource: str, details: dict | None = None) -> dict:
        with self._lock:
            prev = self._entries[-1]["hash"] if self._entries else GENESIS
            e = {"seq": len(self._entries) + 1, "ts": datetime.now(timezone.utc).isoformat(), "actor": actor,
                 "action": action, "resource": resource, "details": details or {}, "prev": prev}
            e["hash"] = self._digest(e)
            self._entries.append(e)
            if self._path:
                with self._path.open("a") as fh:
                    fh.write(json.dumps(e, default=str) + "\n")
            return e

    def entries(self, resource: str | None = None, limit: int | None = None) -> list[dict]:
        out = [e for e in self._entries if resource is None or e["resource"].startswith(resource)]
        return out[-limit:] if limit else list(out)

    def verify(self) -> dict:
        prev = GENESIS
        for i, e in enumerate(self._entries):
            if e["prev"] != prev or e["seq"] != i + 1 or self._digest(e) != e["hash"]:
                return {"valid": False, "broken_at": e["seq"], "entries": len(self._entries)}
            prev = e["hash"]
        return {"valid": True, "entries": len(self._entries), "head": prev}

"""Tamper-evident, signed audit log.

Every entry commits to the previous entry's hash (edits, removals and reordering break the chain) and carries an HMAC signature over its hash
made with a key the log writer holds. Without the key, an attacker who edits the file cannot recompute a valid chain: the signatures fail.
`head()` returns a signed (seq, hash) pair that can be escrowed outside the platform; `verify(expected_head=...)` then also detects
TRUNCATION (dropping the newest entries), which a chain alone cannot see.

Honest limits: this is not WORM storage. Anyone who holds the signing key (it sits next to the log unless RFACTORY_AUDIT_KEY is set) can
forge a consistent log, and entries newer than the last escrowed head can be dropped unnoticed. A production deployment must also ship
entries to immutable storage (object-lock bucket / external SIEM) and keep the key in a KMS/HSM.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def load_audit_key(directory: Path | None) -> bytes | None:
    """RFACTORY_AUDIT_KEY, else a 0600 key file next to the log (development only), else None (unsigned, in-memory logs)."""
    env = os.environ.get("RFACTORY_AUDIT_KEY")
    if env:
        return env.encode()
    if directory is None:
        return None
    kf = Path(directory) / "audit.key"
    if kf.exists():
        return kf.read_bytes().strip()
    key = os.urandom(32).hex().encode()
    fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class AuditLog:
    def __init__(self, path: Path | None = None, key: bytes | None = None):
        self._entries: list[dict] = []
        self._lock = threading.Lock()
        self._path = path
        self._key = key
        if path and path.exists():
            self._entries = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    @staticmethod
    def _digest(e: dict) -> str:
        body = {k: e[k] for k in ("seq", "ts", "actor", "action", "resource", "details", "prev")}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    def _sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()

    def append(self, actor: str, action: str, resource: str, details: dict | None = None) -> dict:
        with self._lock:
            prev = self._entries[-1]["hash"] if self._entries else GENESIS
            e = {"seq": len(self._entries) + 1, "ts": datetime.now(timezone.utc).isoformat(), "actor": actor,
                 "action": action, "resource": resource, "details": details or {}, "prev": prev}
            e["hash"] = self._digest(e)
            if self._key:
                e["sig"] = self._sign(e["hash"])
            self._entries.append(e)
            if self._path:
                with self._path.open("a") as fh:
                    fh.write(json.dumps(e, default=str) + "\n")
            return e

    def entries(self, resource: str | None = None, limit: int | None = None) -> list[dict]:
        out = [e for e in self._entries if resource is None or e["resource"].startswith(resource)]
        return out[-limit:] if limit else list(out)

    def head(self) -> dict:
        """A signed statement of the log's current end: store it somewhere the platform cannot write, to detect truncation later."""
        with self._lock:
            seq = len(self._entries)
            h = self._entries[-1]["hash"] if self._entries else GENESIS
            ts = datetime.now(timezone.utc).isoformat()
            return {"seq": seq, "head": h, "ts": ts, "signature": self._sign(f"{seq}|{h}|{ts}") if self._key else None, "signed": bool(self._key)}

    def verify(self, expected_head: dict | None = None) -> dict:
        prev, signed_seen, legacy = GENESIS, False, 0
        for i, e in enumerate(self._entries):
            if e["prev"] != prev or e["seq"] != i + 1 or self._digest(e) != e["hash"]:
                return {"valid": False, "broken_at": e["seq"], "reason": "hash chain broken", "entries": len(self._entries)}
            if self._key:
                if "sig" in e:
                    signed_seen = True
                    if not hmac.compare_digest(e["sig"], self._sign(e["hash"])):
                        return {"valid": False, "broken_at": e["seq"], "reason": "signature does not verify (entry edited, or signed with another key)", "entries": len(self._entries)}
                elif signed_seen:
                    return {"valid": False, "broken_at": e["seq"], "reason": "unsigned entry after signed ones (signatures stripped)", "entries": len(self._entries)}
                else:
                    legacy += 1  # written before signing existed
            prev = e["hash"]
        out = {"valid": True, "entries": len(self._entries), "head": prev, "signed": bool(self._key), "legacy_unsigned": legacy}
        if expected_head is not None:
            seq = int(expected_head["seq"])
            at = self._entries[seq - 1]["hash"] if 0 < seq <= len(self._entries) else (GENESIS if seq == 0 else None)
            if at != expected_head["head"]:
                return {**out, "valid": False, "reason": "the escrowed head is not part of this log: it was truncated or rewritten"}
            if self._key and expected_head.get("signature") and not hmac.compare_digest(
                    expected_head["signature"], self._sign(f"{seq}|{expected_head['head']}|{expected_head['ts']}")):
                return {**out, "valid": False, "reason": "the escrowed head's signature does not verify"}
            out["escrow_checked"] = True
        return out

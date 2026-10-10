"""Append-only, hash-chained audit trail and evidence packages."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import AuditEvent

GENESIS = "0" * 64


def _ts_str(ts: datetime) -> str:
    """Canonical timestamp: UTC, microsecond precision, no tz suffix (SQLite drops tzinfo on read-back)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat()


def _hash(prev: str, ts: datetime | str, actor: str, action: str, subject_type: str, subject_id: str, details: dict) -> str:
    ts_s = _ts_str(ts) if isinstance(ts, datetime) else ts
    body = json.dumps({"prev": prev, "ts": ts_s, "actor": actor, "action": action, "st": subject_type, "sid": subject_id, "d": details}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def record_event(session: Session, actor: str, action: str, subject_type: str, subject_id: str, details: dict | None = None, tenant_id: str = "default") -> AuditEvent:
    last = session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)).scalars().first()
    prev = last.hash if last else GENESIS
    ts = datetime.now(timezone.utc)
    details = details or {}
    h = _hash(prev, ts, actor, action, subject_type, subject_id, details)
    ev = AuditEvent(ts=ts, tenant_id=tenant_id, actor=actor, action=action, subject_type=subject_type, subject_id=subject_id, details=details, prev_hash=prev, hash=h)
    session.add(ev)
    session.flush()
    return ev


def verify_chain(session: Session) -> dict:
    prev = GENESIS
    n = 0
    for ev in session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars():
        expected = _hash(prev, ev.ts, ev.actor, ev.action, ev.subject_type, ev.subject_id, ev.details)
        if ev.prev_hash != prev or ev.hash != expected:
            return {"ok": False, "events": n, "broken_at": ev.id}
        prev = ev.hash
        n += 1
    return {"ok": True, "events": n, "head": prev}


def write_evidence_package(run_id: str, artifacts: dict[str, str | dict]) -> dict:
    """Write evidence files to the evidence directory and return an index with content hashes."""
    base = os.path.join(config.settings.evidence_dir, run_id)
    os.makedirs(base, exist_ok=True)
    index = {}
    for name, content in artifacts.items():
        path = os.path.join(base, name)
        data = content if isinstance(content, str) else json.dumps(content, indent=2, sort_keys=True, default=str)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data)
        index[name] = {"path": path, "sha256": hashlib.sha256(data.encode()).hexdigest(), "bytes": len(data)}
    with open(os.path.join(base, "evidence_index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    return {"dir": base, "files": index}

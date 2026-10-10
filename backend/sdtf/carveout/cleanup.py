"""Residual cleanup plans: what stays behind in the seller's system after a carve-out, decided item by item,
approved under four eyes, then either executed on the platform's simulated source (the record store) or handed
over as a work package for the SAP-side archiving / deletion run.

The platform never deletes anything on an SAP system: the read-only add-on has no delete, and a live source
only ever receives the package. On the synthetic source the execution removes the record-store rows after a
copy of every affected row has been written to the package, so the result is reproducible and auditable.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..audit.service import record_event
from ..catalog.store import RecordStore, delete_records
from ..catalog.tables import TABLES, record_key
from ..models import ApprovalRecord, MigrationRun, ResidualCleanupPlan, SapSystem, ScopeManifest
from .deals import deal_assessment
from .service import cleanup_candidates

STATUSES = ("DRAFT", "APPROVED", "EXECUTED", "REJECTED")
DECISIONS = ("INCLUDE", "EXCLUDE")
KEEP_ACTION = "FLAG_TRANSFERRED_KEEP"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plans(session: Session, manifest_id: str) -> list[ResidualCleanupPlan]:
    return list(session.execute(select(ResidualCleanupPlan).where(ResidualCleanupPlan.manifest_id == manifest_id).order_by(ResidualCleanupPlan.sequence)).scalars().all())


def _summarise(p: ResidualCleanupPlan) -> dict:
    items = p.items or []
    inc = [i for i in items if i["decision"] == "INCLUDE"]
    p.summary = {
        "items": len(items), "included": len(inc), "excluded": len(items) - len(inc),
        "by_action": {a: sum(1 for i in inc if i["action"] == a) for a in sorted({i["action"] for i in inc})},
        "by_table": {t: sum(1 for i in inc if i["table"] == t) for t in sorted({i["table"] for i in inc})},
        "executed": sum(1 for i in items if i.get("result")),
        "deletes": sum(1 for i in inc if i["action"] != KEEP_ACTION),
    }
    return p.summary


def create_plan(session: Session, m: ScopeManifest, actor: str) -> ResidualCleanupPlan:
    """A plan from the residual exposure of the manifest: every cleanup candidate as an item, included by
    default; under an asset deal every action becomes a flag (the seller keeps its legal record)."""
    assess = deal_assessment(m.definition)
    rule = assess.get("residual_rule") or ""
    cands = cleanup_candidates(session, m)
    items = []
    for i, c in enumerate(cands, start=1):
        action = KEEP_ACTION if rule == "RETAIN_AS_LEGAL_RECORD" else c["action"]
        items.append({"id": f"C{i:05d}", "table": c["table"], "key": c["key"], "object": c["object"], "action": action, "proposed_action": c["action"], "decision": "INCLUDE", "note": "", "decided_by": "", "result": None})
    rows = plans(session, m.id)
    p = ResidualCleanupPlan(project_id=m.project_id, manifest_id=m.id, sequence=(rows[-1].sequence + 1) if rows else 1, status="DRAFT", deal_type=assess.get("deal_type") or "", residual_rule=rule, items=items, created_by=actor, summary={}, package={}, execution={})
    session.add(p)
    session.flush()
    _summarise(p)
    session.flush()
    record_event(session, actor, "CLEANUP_PLAN_CREATED", "MANIFEST", m.id, {"plan_id": p.id, "sequence": p.sequence, "items": len(items), "residual_rule": rule})
    return p


def decide_item(session: Session, p: ResidualCleanupPlan, item_id: str, decision: str, actor: str, note: str = "") -> dict:
    if p.status != "DRAFT":
        raise ValueError(f"plan is {p.status}; items are decided while it is a draft")
    decision = (decision or "").upper()
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if decision == "EXCLUDE" and not note.strip():
        raise ValueError("excluding an item needs a note")
    items = [dict(i) for i in p.items or []]
    it = next((i for i in items if i["id"] == item_id), None)
    if it is None:
        raise LookupError(f"item {item_id} not found")
    it.update(decision=decision, note=note.strip()[:400], decided_by=actor)
    p.items = items
    _summarise(p)
    session.flush()
    record_event(session, actor, "CLEANUP_ITEM_DECIDED", "MANIFEST", p.manifest_id, {"plan_id": p.id, "item": item_id, "decision": decision})
    return it


def _readiness(session: Session, m: ScopeManifest) -> list[str]:
    """Why the source may not be cleaned yet: the data must be transferred and reconciled first."""
    why = []
    if m.status != "APPROVED":
        why.append(f"manifest is {m.status}, not APPROVED")
    runs = [r for r in session.execute(select(MigrationRun).where(MigrationRun.manifest_id == m.id, MigrationRun.status == "COMPLETED")).scalars().all() if (r.metrics or {}).get("kind") != "DELTA"]
    ok = [r for r in runs if ((r.report or {}).get("reconciliation") or {}).get("overall") in ("PASS", "WARN")]
    if not ok:
        why.append("no completed run on the manifest with reconciliation PASS or WARN: the data must be transferred and reconciled before the source is cleaned")
    return why


def approve_plan(session: Session, p: ResidualCleanupPlan, approver: str, comment: str = "") -> ResidualCleanupPlan:
    if approver == p.created_by:
        raise PermissionError("four-eyes principle: the creator of a cleanup plan cannot approve it")
    if p.status != "DRAFT":
        raise ValueError(f"plan is {p.status}")
    m = session.get(ScopeManifest, p.manifest_id)
    why = _readiness(session, m)
    if why:
        raise ValueError("approval refused: " + "; ".join(why))
    p.status, p.approved_by, p.approved_at, p.approval_comment = "APPROVED", approver, _now(), comment[:400]
    session.add(ApprovalRecord(subject_type="CLEANUP_PLAN", subject_id=p.id, decision="APPROVED", decided_by=approver, kind="BUSINESS", comment=comment[:400]))
    session.flush()
    record_event(session, approver, "CLEANUP_PLAN_APPROVED", "MANIFEST", p.manifest_id, {"plan_id": p.id, "included": p.summary.get("included"), "deletes": p.summary.get("deletes")})
    return p


def reject_plan(session: Session, p: ResidualCleanupPlan, approver: str, comment: str = "") -> ResidualCleanupPlan:
    if p.status != "DRAFT":
        raise ValueError(f"plan is {p.status}")
    p.status, p.approved_by, p.approved_at, p.approval_comment = "REJECTED", approver, _now(), comment[:400]
    session.add(ApprovalRecord(subject_type="CLEANUP_PLAN", subject_id=p.id, decision="REJECTED", decided_by=approver, kind="BUSINESS", comment=comment[:400]))
    session.flush()
    record_event(session, approver, "CLEANUP_PLAN_REJECTED", "MANIFEST", p.manifest_id, {"plan_id": p.id, "comment": comment[:200]})
    return p


def _source(session: Session, p: ResidualCleanupPlan) -> SapSystem:
    m = session.get(ScopeManifest, p.manifest_id)
    return session.get(SapSystem, m.definition["source_system_id"])


def _rows_for(session: Session, p: ResidualCleanupPlan) -> dict[str, dict[str, dict]]:
    """{table: {item key: row}} for the included items, from the source record store."""
    src = _source(session, p)
    tables = sorted({i["table"] for i in p.items or [] if i["decision"] == "INCLUDE"})
    store = RecordStore.load(session, src.id, tables=tables) if tables else RecordStore(src.id)
    out: dict[str, dict[str, dict]] = {}
    for t in tables:
        idx = {record_key(t, r): r for r in store.rows(t)}
        out[t] = idx
    return out


def export_package(session: Session, p: ResidualCleanupPlan, out_dir: str | None = None, actor: str = "system") -> dict:
    """The work package: one CSV per table with the affected rows (full payload) and the action, a JSON index with
    hashes, zipped; the hand-over for the SAP-side run, and the copy the simulated execution archives to."""
    src = _source(session, p)
    rows = _rows_for(session, p)
    base = os.path.join(out_dir or os.path.join(config.settings.evidence_dir, "residual"), p.id)
    if os.path.isdir(base):
        shutil.rmtree(base)
    os.makedirs(base, exist_ok=True)
    files: dict[str, dict] = {}

    def put(rel: str, data: bytes) -> None:
        path = os.path.join(base, rel)
        with open(path, "wb") as fh:
            fh.write(data)
        files[rel] = {"sha256": _sha(data), "bytes": len(data)}

    missing = []
    for t in sorted(rows):
        fields = list(TABLES[t].fields)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["ITEM", "ACTION", "OBJECT", *fields])
        for it in p.items or []:
            if it["decision"] != "INCLUDE" or it["table"] != t:
                continue
            r = rows[t].get(it["key"])
            if r is None:
                missing.append(it["id"])
                continue
            w.writerow([it["id"], it["action"], it["object"], *[r.get(f, "") for f in fields]])
        put(f"{t}.csv", buf.getvalue().encode("utf-8"))
    index = {"plan_id": p.id, "manifest_id": p.manifest_id, "sequence": p.sequence, "status": p.status, "deal_type": p.deal_type, "residual_rule": p.residual_rule, "source": {"sid": src.sid, "client": src.client, "connector": src.connector}, "exported_at": _now().isoformat(), "by": actor, "summary": p.summary, "items": [{k: i[k] for k in ("id", "table", "key", "object", "action", "decision")} for i in p.items or []], "missing_in_source": missing, "note": "Rows are the source's record-store payload at export time. The package is the hand-over for the archiving / deletion run on an SAP source; the platform itself deletes only on its simulated source."}
    put("package.json", json.dumps(index, indent=2, default=str).encode("utf-8"))
    zip_path = os.path.join(base, f"residual_{p.id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            zf.write(os.path.join(base, rel), rel)
    p.package = {"dir": base, "zip": zip_path, "files": files, "exported_at": index["exported_at"], "missing_in_source": missing}
    session.flush()
    record_event(session, actor, "CLEANUP_PACKAGE_EXPORTED", "MANIFEST", p.manifest_id, {"plan_id": p.id, "files": sorted(files), "missing": len(missing)})
    return p.package


def execute_plan(session: Session, p: ResidualCleanupPlan, actor: str, out_dir: str | None = None) -> ResidualCleanupPlan:
    """Apply the approved plan on the simulated source: the package is written first (the archive copy), then the
    record-store rows of DELETE / ARCHIVE items are removed; FLAG items change nothing. Refused for a source that is
    reached through the add-on: the platform has no delete there, the package is the deliverable."""
    if p.status != "APPROVED":
        raise ValueError(f"plan is {p.status}; only an APPROVED plan is executed")
    src = _source(session, p)
    if src.connector != "SYNTHETIC":
        raise ValueError(f"the source {src.sid} is reached through the read-only add-on ({src.connector}): the platform cannot delete there; export the work package and hand it to the archiving / deletion run")
    m = session.get(ScopeManifest, p.manifest_id)
    why = _readiness(session, m)
    if why:
        raise ValueError("execution refused: " + "; ".join(why))
    export_package(session, p, out_dir, actor)
    rows = _rows_for(session, p)
    items = [dict(i) for i in p.items or []]
    deleted: dict[str, list[str]] = {}
    for it in items:
        if it["decision"] != "INCLUDE":
            it["result"] = "SKIPPED_EXCLUDED"
            continue
        if it["action"] == KEEP_ACTION:
            it["result"] = "KEPT_FLAGGED"
            continue
        r = rows.get(it["table"], {}).get(it["key"])
        if r is None:
            it["result"] = "NOT_FOUND_IN_SOURCE"
            continue
        deleted.setdefault(it["table"], []).append(record_key(it["table"], r))
        it["result"] = "ARCHIVED_TO_PACKAGE_AND_REMOVED" if it["action"] == "ARCHIVE_AFTER_APPROVAL" else "VIEW_REMOVED"
    removed = {t: delete_records(session, src.id, t, keys) for t, keys in deleted.items()}
    p.items = items
    p.status, p.executed_by, p.executed_at = "EXECUTED", actor, _now()
    p.execution = {"source": {"sid": src.sid, "client": src.client, "connector": src.connector, "simulated": True}, "removed_rows": removed, "results": {r: sum(1 for i in items if i.get("result") == r) for r in sorted({i.get("result") for i in items if i.get("result")})}, "package": p.package.get("zip"), "note": "executed on the platform's simulated source (record store); no SAP system was changed"}
    _summarise(p)
    session.flush()
    record_event(session, actor, "CLEANUP_PLAN_EXECUTED", "MANIFEST", p.manifest_id, {"plan_id": p.id, "removed_rows": removed, "results": p.execution["results"]})
    return p


def plan_out(p: ResidualCleanupPlan, full: bool = True) -> dict:
    d = {"id": p.id, "project_id": p.project_id, "manifest_id": p.manifest_id, "sequence": p.sequence, "status": p.status, "deal_type": p.deal_type, "residual_rule": p.residual_rule, "created_by": p.created_by, "created_at": p.created_at, "approved_by": p.approved_by, "approved_at": p.approved_at, "approval_comment": p.approval_comment, "executed_by": p.executed_by, "executed_at": p.executed_at, "summary": p.summary or _summarise(p), "package": {k: v for k, v in (p.package or {}).items() if k != "dir"}, "execution": p.execution or {}}
    if full:
        d["items"] = p.items or []
    return d


def plan_markdown(p: ResidualCleanupPlan) -> str:
    s = p.summary or _summarise(p)
    md = [f"# Residual cleanup plan {p.sequence} ({p.status})", "", f"Deal type: {p.deal_type or 'none'}; residual rule: {p.residual_rule or 'as reported'}. {s['included']} item(s) included, {s['excluded']} excluded; {s['deletes']} would change the source." + (f" Approved by {p.approved_by}." if p.approved_by and p.status != "REJECTED" else "") + (f" Executed by {p.executed_by} at {p.executed_at.isoformat()} on the simulated source." if p.executed_at else ""), "", "> The platform deletes only on its simulated source; on an SAP source the exported work package is the deliverable for the archiving / deletion run.", "", "| Item | Table | Key | Object | Action | Decision | Note | Result |", "|---|---|---|---|---|---|---|---|"]
    for i in p.items or []:
        md.append(f"| {i['id']} | {i['table']} | {i['key']} | {i['object']} | {i['action']} | {i['decision']} | {(i.get('note') or '').replace('|', '/')} | {i.get('result') or ''} |")
    if p.package:
        md += ["", "## Package", ""] + [f"- {rel}: {meta['bytes']} bytes, sha256 {meta['sha256'][:16]}…" for rel, meta in sorted((p.package.get("files") or {}).items())]
    return "\n".join(md) + "\n"


__all__ = ["STATUSES", "DECISIONS", "KEEP_ACTION", "plans", "create_plan", "decide_item", "approve_plan", "reject_plan", "export_package", "execute_plan", "plan_out", "plan_markdown"]

"""Cutover rehearsal checklist: mock cutovers and dress rehearsals of the runbook, tracked item by item.

A rehearsal (`CutoverRehearsal`) is one pass through the cutover of a manifest: a mock cutover, a dress
rehearsal or the go-live checklist itself. It carries

* a **checklist**: *automatic* items the platform evaluates from its own state (manifest approved, no pending
  dispositions, approved ruleset, completed run, reconciliation, sign-offs, final delta, cockpit rounds
  converged, evidence package, risk assessment, carve-out completeness, target registration) and *manual* items
  the platform cannot see (change freeze, interface stop plan, batch jobs, backups, number ranges, users,
  communication, rollback test, hypercare, residual data, interface switch-over) that someone ticks by hand with
  a note; every tick is audited;
* **task timings**: the runbook tasks started and finished by hand during the rehearsal, so the next runbook
  forecast uses measured minutes where a rehearsal measured them;
* **lessons** recorded during the rehearsal;
* the **verdict**: an approver completes the rehearsal with GO or NO_GO. GO is refused while a blocking item is
  not PASS or NOT_APPLICABLE. Agents never complete a rehearsal.

Everything here is tracking of human steps and of platform state; it never touches an SAP system. The automatic
items say what the platform verified on its simulated runtime, nothing more.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..models import (
    AgentDecision,
    ApprovalRecord,
    CutoverRehearsal,
    MigrationRun,
    RuleSet,
    SapSystem,
    ScopeManifest,
    TransformationException,
)
from ..scope.service import pending_dispositions
from .service import TEMPLATE, generate_runbook

KINDS = ("MOCK", "DRESS", "FINAL")
ITEM_STATUSES = ("PENDING", "PASS", "FAIL", "NOT_APPLICABLE")

# id, phase, title, blocking, owner, runbook task, what the check looks at
AUTO_ITEMS = [
    ("A01", "PREPARE", "Scope manifest approved (four-eyes)", True, "Business", "T02", "manifest status and approver"),
    ("A02", "PREPARE", "No pending business dispositions on the manifest", True, "Business", "T02", "objects still requiring approval"),
    ("A03", "PREPARE", "Transformation ruleset approved", True, "Migration", "T02", "ruleset of the latest completed run"),
    ("A04", "INITIAL_LOAD", "Completed run on this manifest (initial extraction, transformation, load)", True, "Migration", "T04", "latest completed run and its load mode"),
    ("A05", "DOWNTIME", "Reconciliation PASS, or WARN with every variance explained", True, "Migration/Finance", "T08", "reconciliation overall and unexplained non-PASS checks of the latest completed run"),
    ("A06", "INITIAL_LOAD", "No open ERROR exceptions on the latest run", False, "Migration", "T04", "transformation/load exceptions still OPEN"),
    ("A07", "DOWNTIME", "Technical and business sign-off recorded on the reconciliation", True, "Steering", "T09", "approval records of the latest completed run"),
    ("A08", "DOWNTIME", "Source freeze declared and final delta synchronised with full reconciliation PASS", True, "Migration", "T07", "delta state of the baseline run (not applicable when the source cannot capture changes)"),
    ("A09", "INITIAL_LOAD", "Migration cockpit packages simulated and converged (no instance still rejected)", True, "Migration", "T04", "package rounds of the latest run (not applicable when nothing routes to the cockpit)"),
    ("A10", "POST", "Evidence package written for the latest run", False, "PMO", "T13", "evidence files in the run report"),
    ("A11", "PREPARE", "Cutover risk assessed and not HIGH", False, "Steering", "T09", "latest cutover_risk agent decision on the manifest"),
    ("A12", "PREPARE", "Carve-out completeness: every in-scope object classified, no pending approvals", False, "Migration", "T03", "completeness report of the manifest"),
    ("A13", "PREPARE", "Target system registered with its connector", False, "Basis", "T04", "target system of the manifest and its connector status"),
]

MANUAL_ITEMS = [
    ("M01", "PREPARE", "Change freeze and transport lock confirmed in source and target", True, "Basis", "T01"),
    ("M02", "DOWNTIME", "Interface inventory reviewed: stop / switch plan with an owner for every interface", True, "Integration", "T06"),
    ("M03", "DOWNTIME", "Batch jobs and background processing stop list agreed and scheduled", True, "Basis", "T06"),
    ("M04", "DOWNTIME", "Backups / restore points taken on source and target before the window", True, "Basis", "T06"),
    ("M05", "DOWNTIME", "Target number ranges checked (external numbering for carried-over documents, buffers reset)", True, "Basis", "T10"),
    ("M06", "DOWNTIME", "Users locked for the window; emergency and go-live users prepared", True, "Basis/Security", "T06"),
    ("M07", "PREPARE", "Communication plan: window, war room, escalation contacts shared with stakeholders", False, "PMO", "T02"),
    ("M08", "DOWNTIME", "Rollback rehearsed before the point of no return (discard target load, unfreeze source)", True, "Migration", "T10"),
    ("M09", "POST", "Hypercare roster and business validation scripts ready", False, "Business", "T12"),
    ("M10", "POST", "Residual data disposition in the source approved by legal", False, "Legal", "T11"),
    ("M11", "DOWNTIME", "Interface switch-over executed on the rehearsal landscape and verified", False, "Integration", "T10"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def checklist_template() -> list[dict]:
    """The catalogue of items every rehearsal starts from."""
    out = [{"id": i, "phase": ph, "title": t, "kind": "AUTO", "blocking": b, "owner": o, "runbook_task": task, "checks": what} for i, ph, t, b, o, task, what in AUTO_ITEMS]
    out += [{"id": i, "phase": ph, "title": t, "kind": "MANUAL", "blocking": b, "owner": o, "runbook_task": task, "checks": "recorded by hand with a note"} for i, ph, t, b, o, task in MANUAL_ITEMS]
    return out


def _new_items() -> list[dict]:
    return [{**t, "status": "PENDING", "detail": "", "evidence": {}, "checked_by": "", "checked_at": None, "note": ""} for t in checklist_template()]


# ------------------------------------------------------------------------------------------ auto checks
def _latest_completed_run(session: Session, m: ScopeManifest) -> MigrationRun | None:
    rows = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == m.id, MigrationRun.status == "COMPLETED").order_by(MigrationRun.created_at)).scalars().all()
    rows = [r for r in rows if (r.metrics or {}).get("kind") != "DELTA"]
    return rows[-1] if rows else None


def evaluate_auto_items(session: Session, m: ScopeManifest) -> dict[str, tuple[str, str, dict]]:
    """Evaluate every automatic item for the manifest: {item id: (status, detail, evidence)}."""
    out: dict[str, tuple[str, str, dict]] = {}
    out["A01"] = ("PASS" if m.status == "APPROVED" else "FAIL", f"manifest {m.name} v{m.version} is {m.status}" + (f", approved by {m.approved_by}" if m.approved_by else ""), {"manifest_id": m.id, "status": m.status, "approved_by": m.approved_by})
    pend = pending_dispositions(m)
    out["A02"] = ("PASS" if not pend else "FAIL", "no pending dispositions" if not pend else f"{len(pend)} object(s) still require a business disposition", {"pending": len(pend), "samples": pend[:10]})
    run = _latest_completed_run(session, m)
    if run is None:
        out["A03"] = ("FAIL", "no completed run yet, so no ruleset was exercised", {})
        out["A04"] = ("FAIL", "no completed run on this manifest", {"manifest_id": m.id})
        out["A05"] = ("FAIL", "no completed run to reconcile", {})
        out["A06"] = ("FAIL", "no completed run", {})
        out["A07"] = ("FAIL", "no completed run to sign off", {})
        out["A09"] = ("FAIL", "no completed run", {})
        out["A10"] = ("FAIL", "no completed run", {})
    else:
        rs = session.get(RuleSet, run.ruleset_id)
        out["A03"] = ("PASS" if rs and rs.status == "APPROVED" else "FAIL", f"ruleset {rs.name} v{rs.version} is {rs.status}" if rs else "ruleset not found", {"ruleset_id": run.ruleset_id, "status": rs.status if rs else None, "approved_by": rs.approved_by if rs else None})
        load = (run.metrics or {}).get("load_mode") or next((s.metrics.get("load_mode") for s in run.stages if s.name == "LOAD" and s.metrics), None)
        out["A04"] = ("PASS", f"run {run.id[:8]} completed ({run.mode}, load mode {load or 'n/a'}) at {run.finished_at.isoformat() if run.finished_at else '-'}", {"run_id": run.id, "mode": run.mode, "load_mode": load, "stages": {s.name: s.status for s in run.stages}})
        recon = (run.report or {}).get("reconciliation", {}) or {}
        overall = recon.get("overall", "NONE")
        from ..models import ReconciliationResult

        unexplained = session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.status != "PASS", ReconciliationResult.explanation == "")).scalars().all()
        ok = overall == "PASS" or (overall == "WARN" and not unexplained)
        out["A05"] = ("PASS" if ok else "FAIL", f"reconciliation overall {overall}" + (f", {len(unexplained)} non-PASS check(s) without explanation" if unexplained else (", every variance explained" if overall == "WARN" else "")), {"run_id": run.id, "overall": overall, "unexplained": len(unexplained), "layers": recon.get("layers") or recon.get("by_layer")})
        open_err = session.execute(select(TransformationException).where(TransformationException.run_id == run.id, TransformationException.disposition == "OPEN", TransformationException.severity == "ERROR")).scalars().all()
        out["A06"] = ("PASS" if not open_err else "FAIL", "no open ERROR exceptions" if not open_err else f"{len(open_err)} open ERROR exception(s): " + ", ".join(sorted({e.stage for e in open_err})), {"run_id": run.id, "open_errors": len(open_err), "by_stage": {st: sum(1 for e in open_err if e.stage == st) for st in sorted({e.stage for e in open_err})}})
        appr = session.execute(select(ApprovalRecord).where(ApprovalRecord.subject_type == "RECONCILIATION", ApprovalRecord.subject_id == run.id, ApprovalRecord.decision == "APPROVED")).scalars().all()
        kinds = {a.kind: a.decided_by for a in appr}
        missing = [k for k in ("TECHNICAL", "BUSINESS") if k not in kinds]
        out["A07"] = ("PASS" if not missing else "FAIL", "technical and business sign-off recorded" if not missing else f"missing sign-off: {', '.join(missing)}", {"run_id": run.id, "signed": kinds})
        try:
            from ..runtime.cockpit_attempts import burndown

            cockpit_rows = next(((s.metrics.get("by_method") or {}).get("MIGRATION_COCKPIT", 0) for s in run.stages if s.name == "LOAD" and s.metrics), 0) or 0
            bd = burndown(session, run.id)
            if not bd["rounds"]:
                out["A09"] = ("NOT_APPLICABLE" if not cockpit_rows else "FAIL", "nothing routed to the migration cockpit" if not cockpit_rows else f"{cockpit_rows} cockpit row(s) loaded but no package exported yet", {"run_id": run.id, "cockpit_rows": cockpit_rows})
            else:
                out["A09"] = ("PASS" if bd["converged"] else "FAIL", f"{len(bd['rounds'])} round(s), {bd['remaining']} instance(s) still rejected" + (", converged" if bd["converged"] else (", no round simulated yet" if not bd["line"] else "")), {"run_id": run.id, "rounds": len(bd["rounds"]), "remaining": bd["remaining"], "converged": bd["converged"], "current": (bd["current"] or {}).get("status")})
        except Exception as e:  # pragma: no cover - defensive: the checklist must never fail because of a sub-system
            out["A09"] = ("FAIL", f"cockpit rounds could not be read: {e}", {})
        files = ((run.report or {}).get("evidence") or {}).get("files") or {}
        out["A10"] = ("PASS" if files else "FAIL", f"{len(files)} evidence file(s)" if files else "no evidence package in the run report", {"run_id": run.id, "files": sorted(files)[:20]})
    # final delta
    src = session.get(SapSystem, m.definition.get("source_system_id"))
    if run is None:
        out["A08"] = ("FAIL", "no baseline run", {})
    elif src is None or src.connector != "RFC":
        out["A08"] = ("NOT_APPLICABLE", f"source connector {src.connector if src else '?'} cannot capture changes; the cutover is a full-load cutover without delta", {"connector": src.connector if src else None})
    else:
        from ..runtime.delta import delta_state

        st = delta_state(session, run, with_backlog=False)
        fd = st.get("final_delta") or {}
        out["A08"] = ("PASS" if st.get("cutover_ready") else "FAIL", ("freeze declared, final delta reconciled PASS" if st.get("cutover_ready") else ("freeze declared, final delta " + (fd.get("reconciliation") or "not run") if st.get("freeze") else "freeze not declared")), {"baseline_run_id": run.id, "freeze": bool(st.get("freeze")), "final_delta": fd.get("reconciliation"), "cycles": len(st.get("cycles") or [])})
    # risk
    dec = session.execute(select(AgentDecision).where(AgentDecision.agent == "cutover_risk", AgentDecision.subject_id == m.id).order_by(AgentDecision.created_at.desc())).scalars().first()
    if dec is None:
        out["A11"] = ("FAIL", "cutover risk not assessed yet (run the cutover_risk agent)", {})
    else:
        band = (dec.proposal or {}).get("band")
        out["A11"] = ("PASS" if band in ("LOW", "MEDIUM") else "FAIL", f"risk {band} (score {(dec.proposal or {}).get('score')}) assessed at {dec.created_at.isoformat() if dec.created_at else '-'}", {"decision_id": dec.id, "band": band, "score": (dec.proposal or {}).get("score")})
    # completeness
    try:
        from ..carveout.service import completeness_report

        comp = completeness_report(session, m)
        out["A12"] = ("PASS" if comp.get("complete") else "FAIL", "complete" if comp.get("complete") else f"incomplete: {comp.get('pending_approvals', {}).get('count', 0)} pending approval(s)", {"complete": comp.get("complete"), "pending_approvals": comp.get("pending_approvals", {}).get("count")})
    except Exception as e:
        out["A12"] = ("FAIL", f"completeness report unavailable: {e}", {})
    tgt = session.get(SapSystem, m.definition.get("target_system_id"))
    out["A13"] = ("PASS" if tgt else "FAIL", f"target {tgt.sid}/{tgt.client} ({tgt.product} {tgt.release}) via {tgt.connector}, connector status {tgt.connector_status}" if tgt else "no target system on the manifest", {"system_id": tgt.id if tgt else None, "connector": tgt.connector if tgt else None, "connector_status": tgt.connector_status if tgt else None})
    return out


# ------------------------------------------------------------------------------------------- lifecycle
def rehearsals(session: Session, manifest_id: str) -> list[CutoverRehearsal]:
    return list(session.execute(select(CutoverRehearsal).where(CutoverRehearsal.manifest_id == manifest_id).order_by(CutoverRehearsal.sequence)).scalars().all())


def create_rehearsal(session: Session, m: ScopeManifest, name: str, kind: str, actor: str) -> CutoverRehearsal:
    """A new rehearsal of the manifest's runbook: the checklist with the automatic items evaluated now."""
    kind = (kind or "MOCK").upper()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    rows = rehearsals(session, m.id)
    seq = (rows[-1].sequence + 1) if rows else 1
    rb = generate_runbook(session, m)
    r = CutoverRehearsal(project_id=m.project_id, manifest_id=m.id, sequence=seq, name=(name or f"{kind.title()} rehearsal {seq}")[:128], kind=kind, status="PLANNED", items=_new_items(), runbook=[{k: t[k] for k in ("id", "name", "phase", "depends_on", "est_minutes", "downtime", "irreversible", "owner")} for t in rb["tasks"]], timings={}, lessons=[], summary={}, created_by=actor)
    session.add(r)
    session.flush()
    refresh_auto_items(session, r, actor, audit=False)
    record_event(session, actor, "CUTOVER_REHEARSAL_CREATED", "MANIFEST", m.id, {"rehearsal_id": r.id, "sequence": seq, "kind": kind, "name": r.name})
    return r


def _summarise(r: CutoverRehearsal) -> dict:
    items = r.items or []
    by = {s: sum(1 for i in items if i["status"] == s) for s in ITEM_STATUSES}
    blocking_open = [i["id"] for i in items if i["blocking"] and i["status"] not in ("PASS", "NOT_APPLICABLE")]
    timed = {k: v for k, v in (r.timings or {}).items() if v.get("actual_minutes") is not None}
    est = {t["id"]: t["est_minutes"] for t in r.runbook or []}
    r.summary = {"items": len(items), **{s.lower(): n for s, n in by.items()}, "blocking_open": blocking_open, "ready_for_go": not blocking_open, "tasks_timed": len(timed), "actual_minutes": round(sum(v["actual_minutes"] for v in timed.values()), 1), "estimated_minutes_of_timed": round(sum(est.get(k, 0) for k in timed), 1), "downtime_actual_minutes": round(sum(v["actual_minutes"] for k, v in timed.items() if next((t["downtime"] for t in r.runbook or [] if t["id"] == k), False)), 1)}
    return r.summary


def refresh_auto_items(session: Session, r: CutoverRehearsal, actor: str, audit: bool = True) -> dict:
    """Re-evaluate the automatic items from the platform state (waived items stay waived)."""
    if r.status in ("COMPLETED", "ABORTED"):
        raise ValueError(f"rehearsal is {r.status}; its checklist is frozen")
    m = session.get(ScopeManifest, r.manifest_id)
    res = evaluate_auto_items(session, m)
    changed = []
    items = []
    for it in r.items:
        it = dict(it)
        if it["kind"] == "AUTO" and it["id"] in res and not it.get("waived"):
            status, detail, evidence = res[it["id"]]
            if status != it["status"]:
                changed.append({"id": it["id"], "from": it["status"], "to": status})
            it.update(status=status, detail=detail, evidence=evidence, checked_by="platform", checked_at=_now().isoformat())
        items.append(it)
    r.items = items
    _summarise(r)
    session.flush()
    if audit:
        record_event(session, actor, "CUTOVER_REHEARSAL_REFRESHED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "changed": changed, "blocking_open": r.summary["blocking_open"]})
    return {"changed": changed, "summary": r.summary}


def start_rehearsal(session: Session, r: CutoverRehearsal, actor: str, note: str = "") -> CutoverRehearsal:
    if r.status != "PLANNED":
        raise ValueError(f"rehearsal is {r.status}; only a PLANNED rehearsal can be started")
    r.status, r.started_at, r.started_by, r.start_note = "IN_PROGRESS", _now(), actor, note[:400]
    session.flush()
    record_event(session, actor, "CUTOVER_REHEARSAL_STARTED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "sequence": r.sequence})
    return r


def mark_item(session: Session, r: CutoverRehearsal, item_id: str, status: str, actor: str, note: str = "") -> dict:
    """Tick a manual item (PASS / FAIL / NOT_APPLICABLE / back to PENDING). An automatic item can only be waived
    to NOT_APPLICABLE with a note (recorded as a waiver) or un-waived."""
    if r.status in ("COMPLETED", "ABORTED"):
        raise ValueError(f"rehearsal is {r.status}; its checklist is frozen")
    status = status.upper()
    if status not in ITEM_STATUSES:
        raise ValueError(f"status must be one of {', '.join(ITEM_STATUSES)}")
    items = [dict(i) for i in r.items]
    it = next((i for i in items if i["id"] == item_id), None)
    if it is None:
        raise ValueError(f"item {item_id} not found")
    if it["kind"] == "AUTO":
        if status == "NOT_APPLICABLE":
            if not note.strip():
                raise ValueError("waiving an automatic item needs a note")
            it.update(status="NOT_APPLICABLE", waived=True, note=note[:400], checked_by=actor, checked_at=_now().isoformat(), detail=f"waived by {actor}: {note[:200]}")
        elif status == "PENDING":
            it.update(waived=False, note=note[:400], checked_by=actor, checked_at=_now().isoformat())
            r.items = items
            refresh_auto_items(session, r, actor, audit=False)
            record_event(session, actor, "CUTOVER_ITEM_UNWAIVED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "item": item_id})
            return next(i for i in r.items if i["id"] == item_id)
        else:
            raise ValueError(f"item {item_id} is evaluated by the platform; it can only be waived (NOT_APPLICABLE with a note) or refreshed")
    else:
        if status == "FAIL" and not note.strip():
            raise ValueError("a failed item needs a note saying what failed")
        it.update(status=status, note=note[:400], checked_by=actor if status != "PENDING" else "", checked_at=_now().isoformat() if status != "PENDING" else None, detail=note[:200] if status != "PENDING" else "")
    r.items = items
    _summarise(r)
    session.flush()
    record_event(session, actor, "CUTOVER_ITEM_MARKED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "item": item_id, "status": status, "note": note[:200], "waived": bool(it.get("waived"))})
    return it


def time_task(session: Session, r: CutoverRehearsal, task_id: str, action: str, actor: str, note: str = "") -> dict:
    """Record by hand that a runbook task started or finished during the rehearsal."""
    if r.status != "IN_PROGRESS":
        raise ValueError(f"rehearsal is {r.status}; tasks are timed while it is IN_PROGRESS")
    if task_id not in {t["id"] for t in r.runbook or []}:
        raise ValueError(f"task {task_id} is not in the runbook")
    timings = {k: dict(v) for k, v in (r.timings or {}).items()}
    t = timings.setdefault(task_id, {"started_at": None, "finished_at": None, "actual_minutes": None, "by": "", "note": ""})
    now = _now()
    if action == "start":
        if t["started_at"] and not t["finished_at"]:
            raise ValueError(f"task {task_id} already started")
        t.update(started_at=now.isoformat(), finished_at=None, actual_minutes=None, by=actor, note=note[:400])
    elif action == "finish":
        if not t["started_at"]:
            raise ValueError(f"task {task_id} was not started")
        if t["finished_at"]:
            raise ValueError(f"task {task_id} already finished")
        started = datetime.fromisoformat(t["started_at"])
        t.update(finished_at=now.isoformat(), actual_minutes=round(max((now - started).total_seconds() / 60.0, 0.0), 2), by=actor, note=(note or t.get("note", ""))[:400])
    else:
        raise ValueError("action must be start or finish")
    r.timings = timings
    _summarise(r)
    session.flush()
    record_event(session, actor, f"CUTOVER_TASK_{action.upper()}ED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "task": task_id, "actual_minutes": t["actual_minutes"]})
    return t


def add_lesson(session: Session, r: CutoverRehearsal, text: str, actor: str, task_id: str = "") -> dict:
    if not text.strip():
        raise ValueError("lesson text is empty")
    lesson = {"text": text.strip()[:1000], "by": actor, "at": _now().isoformat(), "task": task_id}
    r.lessons = list(r.lessons or []) + [lesson]
    session.flush()
    record_event(session, actor, "CUTOVER_LESSON_ADDED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "task": task_id})
    return lesson


def complete_rehearsal(session: Session, r: CutoverRehearsal, verdict: str, actor: str, note: str = "") -> CutoverRehearsal:
    """The approver's verdict. GO needs every blocking item PASS or NOT_APPLICABLE; the automatic items are
    re-evaluated first so a GO never rests on stale state."""
    verdict = verdict.upper()
    if verdict not in ("GO", "NO_GO"):
        raise ValueError("verdict must be GO or NO_GO")
    if r.status not in ("PLANNED", "IN_PROGRESS"):
        raise ValueError(f"rehearsal is {r.status}")
    refresh_auto_items(session, r, actor, audit=False)
    open_items = r.summary["blocking_open"]
    if verdict == "GO" and open_items:
        raise ValueError(f"GO refused: blocking item(s) not passed: {', '.join(open_items)}")
    r.status, r.verdict, r.completed_at, r.completed_by, r.completion_note = "COMPLETED", verdict, _now(), actor, note[:400]
    session.flush()
    session.add(ApprovalRecord(subject_type="REHEARSAL", subject_id=r.id, decision="APPROVED" if verdict == "GO" else "REJECTED", decided_by=actor, kind="BUSINESS" if r.kind == "FINAL" else "TECHNICAL", comment=note[:400]))
    record_event(session, actor, "CUTOVER_REHEARSAL_COMPLETED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "sequence": r.sequence, "verdict": verdict, "failed_items": [i["id"] for i in r.items if i["status"] == "FAIL"], "actual_minutes": r.summary.get("actual_minutes")})
    return r


def abort_rehearsal(session: Session, r: CutoverRehearsal, actor: str, note: str = "") -> CutoverRehearsal:
    if r.status in ("COMPLETED", "ABORTED"):
        raise ValueError(f"rehearsal is {r.status}")
    r.status, r.completed_at, r.completed_by, r.completion_note = "ABORTED", _now(), actor, note[:400]
    session.flush()
    record_event(session, actor, "CUTOVER_REHEARSAL_ABORTED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "sequence": r.sequence, "note": note[:200]})
    return r


# --------------------------------------------------------------------------------------------- outputs
def rehearsal_actuals(session: Session, manifest_id: str) -> dict[str, dict]:
    """Measured minutes per runbook task from the latest completed rehearsal that timed the task."""
    out: dict[str, dict] = {}
    for r in rehearsals(session, manifest_id):
        if r.status != "COMPLETED":
            continue
        for task_id, t in (r.timings or {}).items():
            if t.get("actual_minutes") is not None:
                out[task_id] = {"actual_minutes": t["actual_minutes"], "rehearsal": r.sequence, "name": r.name}
    return out


def rehearsal_out(r: CutoverRehearsal, full: bool = True) -> dict:
    d = {"id": r.id, "project_id": r.project_id, "manifest_id": r.manifest_id, "sequence": r.sequence, "name": r.name, "kind": r.kind, "status": r.status, "verdict": r.verdict, "created_by": r.created_by, "created_at": r.created_at, "started_at": r.started_at, "started_by": r.started_by, "completed_at": r.completed_at, "completed_by": r.completed_by, "completion_note": r.completion_note, "summary": r.summary or _summarise(r)}
    if full:
        d.update(items=r.items, runbook=r.runbook, timings=r.timings or {}, lessons=r.lessons or [])
    return d


def rehearsal_markdown(r: CutoverRehearsal) -> str:
    s = r.summary or _summarise(r)
    md = [f"# Cutover rehearsal {r.sequence}: {r.name} ({r.kind})", "", f"Status **{r.status}**" + (f", verdict **{r.verdict}** by {r.completed_by} at {r.completed_at.isoformat() if r.completed_at else '-'}" if r.verdict else "") + f". Items: {s['pass']} PASS, {s['fail']} FAIL, {s['not_applicable']} N/A, {s['pending']} pending; blocking open: {', '.join(s['blocking_open']) or 'none'}.", "", "> The automatic items reflect the platform's own state on its simulated runtime; manual items are what people recorded. No SAP system was read for this checklist.", "", "| # | Phase | Item | Kind | Blocking | Status | Detail | By |", "|---|---|---|---|---|---|---|---|"]
    for i in r.items:
        md.append(f"| {i['id']} | {i['phase']} | {i['title']} | {i['kind']} | {'yes' if i['blocking'] else ''} | {i['status']} | {(i.get('detail') or i.get('note') or '').replace('|', '/')} | {i.get('checked_by') or ''} |")
    if r.timings:
        est = {t["id"]: t for t in r.runbook or []}
        md += ["", "## Task timings", "", "| Task | Estimate (min) | Actual (min) | Started | Finished | Note |", "|---|---|---|---|---|---|"]
        for k, t in sorted(r.timings.items()):
            md.append(f"| {k} {est.get(k, {}).get('name', '')} | {est.get(k, {}).get('est_minutes', '')} | {t.get('actual_minutes') if t.get('actual_minutes') is not None else '-'} | {t.get('started_at') or ''} | {t.get('finished_at') or ''} | {(t.get('note') or '').replace('|', '/')} |")
        md.append(f"\nTimed tasks: estimate {s['estimated_minutes_of_timed']} min, actual {s['actual_minutes']} min (downtime tasks {s['downtime_actual_minutes']} min).")
    if r.lessons:
        md += ["", "## Lessons", ""] + [f"- {l['text']} ({l['by']}, {l['at'][:16]}{', task ' + l['task'] if l.get('task') else ''})" for l in r.lessons]
    if r.completion_note:
        md += ["", f"Completion note: {r.completion_note}"]
    return "\n".join(md) + "\n"


__all__ = ["AUTO_ITEMS", "MANUAL_ITEMS", "KINDS", "ITEM_STATUSES", "TEMPLATE", "checklist_template", "evaluate_auto_items", "rehearsals", "create_rehearsal", "refresh_auto_items", "start_rehearsal", "mark_item", "time_task", "add_lesson", "complete_rehearsal", "abort_rehearsal", "rehearsal_actuals", "rehearsal_out", "rehearsal_markdown"]

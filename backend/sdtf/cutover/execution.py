"""Live execution tracking of a cutover rehearsal or go-live: the runbook timeline against the clock, task
timings observed from the platform's own runs where the platform did the work, incidents with an escalation
path, and the people assigned to the tasks.

What the platform observes is limited to what it executed itself (initial run, delta cycles, final delta and
its reconciliation); every other task is timed by hand. Nothing here touches an SAP system or pages anyone:
escalation is a recorded step with a named level, the paging is done by people.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..models import CutoverRehearsal, MigrationRun, ScopeManifest

SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
INCIDENT_STATUSES = ("OPEN", "ESCALATED", "RESOLVED")
BLOCKING_SEVERITIES = ("HIGH", "CRITICAL")

# escalation path per task owner: level 1 is the owner's lead, the last level the steering committee
ESCALATION_PATHS = {
    "Basis": ["Basis lead", "IT operations manager", "Cutover manager", "Steering committee"],
    "Basis/Security": ["Basis lead", "Security officer", "Cutover manager", "Steering committee"],
    "Business": ["Business process owner", "Business cutover lead", "Steering committee"],
    "Business/Basis": ["Business cutover lead", "Basis lead", "Cutover manager", "Steering committee"],
    "Migration": ["Migration lead", "Cutover manager", "Steering committee"],
    "Migration/Finance": ["Migration lead", "Finance lead", "Cutover manager", "Steering committee"],
    "Migration/Legal": ["Migration lead", "Legal counsel", "Steering committee"],
    "Integration": ["Integration lead", "Cutover manager", "Steering committee"],
    "Steering": ["Steering committee"],
    "PMO": ["PMO lead", "Steering committee"],
    "Legal": ["Legal counsel", "Steering committee"],
}
DEFAULT_PATH = ["Cutover manager", "Steering committee"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _aware(d: datetime | None) -> datetime | None:
    """Database timestamps come back naive from SQLite; everything here is UTC."""
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def escalation_path(owner: str) -> list[str]:
    return list(ESCALATION_PATHS.get(owner or "", DEFAULT_PATH))


# ------------------------------------------------------------------------------ observed from the platform
def _baseline_run(session: Session, manifest_id: str) -> MigrationRun | None:
    rows = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == manifest_id, MigrationRun.status == "COMPLETED").order_by(MigrationRun.created_at)).scalars().all()
    rows = [r for r in rows if (r.metrics or {}).get("kind") != "DELTA"]
    return rows[-1] if rows else None


def _span(started: datetime | None, finished: datetime | None, source: str) -> dict | None:
    started, finished = _aware(started), _aware(finished)
    if not started or not finished:
        return None
    return {"started_at": _iso(started), "finished_at": _iso(finished), "actual_minutes": round(max((finished - started).total_seconds() / 60.0, 0.0), 2), "by": "platform", "observed": True, "source": source}


def observed_timings(session: Session, r: CutoverRehearsal) -> dict[str, dict]:
    """Task timings the platform saw itself: the initial run (T04), the delta cycles (T05), the final delta (T07)
    and the final reconciliation (T08). Tasks people do are not here."""
    out: dict[str, dict] = {}
    base = _baseline_run(session, r.manifest_id)
    if base is None:
        return out
    stages = {s.name: s for s in base.stages}
    first = min((_aware(s.started_at) for s in base.stages if s.started_at), default=_aware(base.started_at))
    load = stages.get("LOAD")
    span = _span(first, (load.finished_at if load else None) or base.finished_at, f"run {base.id[:8]}: stages {' → '.join(s.name for s in base.stages if s.name in ('EXTRACT', 'TRANSFORM', 'LOAD'))}")
    if span:
        out["T04"] = span
    cycles = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == r.manifest_id).order_by(MigrationRun.created_at)).scalars().all()
    cycles = [c for c in cycles if (c.metrics or {}).get("kind") == "DELTA" and (c.metrics or {}).get("baseline_run_id") == base.id and c.status == "COMPLETED"]
    regular = [c for c in cycles if not (c.metrics or {}).get("final")]
    final = next((c for c in cycles if (c.metrics or {}).get("final")), None)
    if regular:
        span = _span(regular[0].started_at, regular[-1].finished_at, f"{len(regular)} delta cycle(s) {regular[0].id[:8]} … {regular[-1].id[:8]}")
        if span:
            out["T05"] = span
    if final:
        fst = {s.name: s for s in final.stages}
        rec = fst.get("RECONCILE")
        span = _span(final.started_at, (rec.started_at if rec and rec.started_at else final.finished_at), f"final delta {final.id[:8]}: capture → apply")
        if span:
            out["T07"] = span
        span = _span(rec.started_at if rec else None, rec.finished_at if rec else None, f"final delta {final.id[:8]}: RECONCILE stage")
        if span:
            out["T08"] = span
    elif stages.get("RECONCILE"):
        rec = stages["RECONCILE"]
        span = _span(rec.started_at, rec.finished_at, f"run {base.id[:8]}: RECONCILE stage (no delta on this source)")
        if span:
            out["T08"] = span
    return out


# ------------------------------------------------------------------------------------------- timeline
def _schedule(tasks: list[dict]) -> dict[str, tuple[float, float]]:
    """Earliest start / finish in minutes from the dependency graph and the estimates (runbook order is topological)."""
    finish: dict[str, float] = {}
    out: dict[str, tuple[float, float]] = {}
    for t in tasks:
        start = max((finish[d] for d in t.get("depends_on", []) if d in finish), default=0.0)
        finish[t["id"]] = start + float(t.get("est_minutes", 0))
        out[t["id"]] = (round(start, 1), round(finish[t["id"]], 1))
    return out


def effective_timings(session: Session, r: CutoverRehearsal) -> dict[str, dict]:
    """Hand timings win (people time the whole task); where nobody timed a task the platform's observation stands,
    provided it happened after the rehearsal started. An observation from before the start is history: shown with
    the task, never counted as the task done in this rehearsal."""
    obs = observed_timings(session, r)
    out = {}
    for t in r.runbook or []:
        o = obs.get(t["id"])
        in_window = bool(o and r.started_at and _parse(o["started_at"]) >= _aware(r.started_at))
        hand = (r.timings or {}).get(t["id"])
        if hand and hand.get("started_at"):
            out[t["id"]] = {**hand, "observed": False, "also_observed": o if in_window else None, "history": None if in_window else o}
        elif in_window:
            out[t["id"]] = {**o, "history": None}
        elif o:
            out[t["id"]] = {"history": o}
    return out


def timeline(session: Session, r: CutoverRehearsal, now: datetime | None = None) -> dict:
    """The runbook against the clock: planned window per task from the rehearsal start, actual start / finish
    (by hand or observed), status, lateness, the projection of the rest from what has happened, the downtime
    clock, open incidents and the assignments."""
    now = _aware(now) or _now()
    tasks = r.runbook or []
    plan = _schedule(tasks)
    t0 = _aware(r.started_at)
    timings = effective_timings(session, r)
    incidents = list(r.incidents or [])
    assignments = dict(r.assignments or {})
    rows = []
    done: dict[str, datetime] = {}
    projected_finish: dict[str, datetime] = {}
    for t in tasks:
        tid = t["id"]
        ps, pf = plan[tid]
        tm = timings.get(tid) or {}
        started, finished = _parse(tm.get("started_at")), _parse(tm.get("finished_at"))
        planned_start = t0 + timedelta(minutes=ps) if t0 else None
        planned_finish = t0 + timedelta(minutes=pf) if t0 else None
        deps_done = all(d in done for d in t.get("depends_on", []))
        if finished:
            status = "DONE"
            done[tid] = finished
            projected_finish[tid] = finished
        elif started:
            status = "RUNNING"
            projected_finish[tid] = max(now, started + timedelta(minutes=float(t["est_minutes"])))
        else:
            status = "READY" if deps_done and r.status == "IN_PROGRESS" else "WAITING"
            dep_end = max((projected_finish[d] for d in t.get("depends_on", []) if d in projected_finish), default=None)
            base = max(dep_end, now) if dep_end else (now if r.status == "IN_PROGRESS" else (planned_start or now))
            projected_finish[tid] = base + timedelta(minutes=float(t["est_minutes"]))
        late = 0.0
        if r.status == "IN_PROGRESS" and planned_finish:
            if status == "DONE" and finished and finished > planned_finish:
                late = (finished - planned_finish).total_seconds() / 60.0
            elif status in ("RUNNING", "READY") and now > planned_finish:
                late = (now - planned_finish).total_seconds() / 60.0
        open_inc = [i for i in incidents if i.get("task") == tid and i.get("status") != "RESOLVED"]
        rows.append({
            "id": tid, "name": t["name"], "phase": t["phase"], "owner": t["owner"], "downtime": t["downtime"], "irreversible": t.get("irreversible", False), "depends_on": t.get("depends_on", []),
            "est_minutes": t["est_minutes"], "planned_start": _iso(planned_start), "planned_finish": _iso(planned_finish), "planned_offset_min": ps,
            "status": status, "started_at": _iso(started), "finished_at": _iso(finished), "actual_minutes": tm.get("actual_minutes"), "timed_by": tm.get("by", ""), "observed": bool(tm.get("observed")), "source": tm.get("source", ""), "history": tm.get("history"),
            "variance_minutes": round(tm["actual_minutes"] - float(t["est_minutes"]), 1) if tm.get("actual_minutes") is not None else None,
            "late_minutes": round(late, 1), "projected_finish": _iso(projected_finish[tid]),
            "assignee": (assignments.get(tid) or {}).get("assignee", ""), "backup": (assignments.get(tid) or {}).get("backup", ""), "contact": (assignments.get(tid) or {}).get("contact", ""),
            "open_incidents": len(open_inc), "highest_open_severity": max((i["severity"] for i in open_inc), key=SEVERITIES.index, default=None),
        })
    # downtime clock: from the first downtime task started to the last downtime task finished
    # downtime clock: from the business freeze (the first downtime task of the runbook) started in this rehearsal
    # to the last downtime task finished; a stage the platform ran before the freeze does not start the clock
    dt_tasks = [x for x in rows if x["downtime"]]
    first_dt = dt_tasks[0] if dt_tasks else None
    dt_start = _parse(first_dt["started_at"]) if first_dt and first_dt["started_at"] and t0 and _parse(first_dt["started_at"]) >= t0 else None
    dt_end = max((_parse(x["finished_at"]) for x in dt_tasks if x["finished_at"]), default=None) if dt_start and all(x["status"] == "DONE" for x in dt_tasks) else None
    dt_planned = sum(float(x["est_minutes"]) for x in dt_tasks)
    dt_projected = ((max(projected_finish[x["id"]] for x in dt_tasks) - dt_start).total_seconds() / 60.0) if dt_start else dt_planned
    open_incidents = [i for i in incidents if i.get("status") != "RESOLVED"]
    blocking = [i for i in open_incidents if i.get("severity") in BLOCKING_SEVERITIES]
    unassigned_dt = [x["id"] for x in dt_tasks if not x["assignee"]]
    end_planned = max(pf for _, pf in plan.values()) if plan else 0.0
    end_projected = max(projected_finish.values()) if projected_finish else now
    return {
        "rehearsal_id": r.id, "status": r.status, "verdict": r.verdict, "now": _iso(now), "started_at": _iso(t0),
        "elapsed_minutes": round((now - t0).total_seconds() / 60.0, 1) if t0 and r.status == "IN_PROGRESS" else (round(((_aware(r.completed_at) or now) - t0).total_seconds() / 60.0, 1) if t0 else 0.0),
        "planned_total_minutes": round(end_planned, 1),
        "projected_end": _iso(end_projected), "projected_total_minutes": round((end_projected - t0).total_seconds() / 60.0, 1) if t0 else round(end_planned, 1),
        "counts": {s: sum(1 for x in rows if x["status"] == s) for s in ("DONE", "RUNNING", "READY", "WAITING")},
        "late": [x["id"] for x in rows if x["late_minutes"] > 0],
        "observed_tasks": [x["id"] for x in rows if x["observed"]],
        "next": [x["id"] for x in rows if x["status"] == "READY"][:5],
        "downtime": {"started_at": _iso(dt_start), "ended_at": _iso(dt_end), "elapsed_minutes": round(((dt_end or now) - dt_start).total_seconds() / 60.0, 1) if dt_start else 0.0, "planned_minutes": round(dt_planned, 1), "projected_minutes": round(dt_projected, 1), "running": bool(dt_start and not dt_end)},
        "incidents": {"open": len(open_incidents), "blocking": [i["id"] for i in blocking], "by_severity": {s: sum(1 for i in open_incidents if i.get("severity") == s) for s in SEVERITIES}},
        "assignments": {"assigned": sum(1 for x in rows if x["assignee"]), "tasks": len(rows), "unassigned_downtime": unassigned_dt},
        "tasks": rows,
    }


# ------------------------------------------------------------------------------------------- incidents
def blocking_incidents(r: CutoverRehearsal) -> list[str]:
    return [i["id"] for i in (r.incidents or []) if i.get("status") != "RESOLVED" and i.get("severity") in BLOCKING_SEVERITIES]


def _task(r: CutoverRehearsal, task_id: str) -> dict:
    t = next((t for t in r.runbook or [] if t["id"] == task_id), None)
    if t is None:
        raise ValueError(f"task {task_id} is not in the runbook")
    return t


def _resummarise(r: CutoverRehearsal) -> None:
    from .rehearsal import _summarise

    _summarise(r)


def _live(r: CutoverRehearsal) -> None:
    if r.status not in ("PLANNED", "IN_PROGRESS"):
        raise ValueError(f"rehearsal is {r.status}; its execution record is frozen")


def raise_incident(session: Session, r: CutoverRehearsal, task_id: str, severity: str, title: str, actor: str, detail: str = "") -> dict:
    """An incident on a runbook task during the rehearsal: its escalation path follows the task's owner. A HIGH
    or CRITICAL incident blocks GO until it is resolved; a CRITICAL one is escalated to the first level at once."""
    _live(r)
    t = _task(r, task_id)
    severity = (severity or "").upper()
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not title.strip():
        raise ValueError("an incident needs a title")
    path = escalation_path(t["owner"])
    now = _now()
    inc = {"id": f"I{len(r.incidents or []) + 1:02d}-{uuid.uuid4().hex[:6]}", "task": task_id, "owner": t["owner"], "severity": severity, "title": title.strip()[:200], "detail": detail.strip()[:1000], "status": "OPEN", "raised_by": actor, "raised_at": now.isoformat(), "path": path, "level": 0, "escalations": [], "resolved_by": "", "resolved_at": None, "resolution": ""}
    if severity == "CRITICAL":
        inc.update(status="ESCALATED", level=1, escalations=[{"level": 1, "to": path[0], "by": "platform", "at": now.isoformat(), "note": "critical incidents are escalated at once"}])
    r.incidents = list(r.incidents or []) + [inc]
    _resummarise(r)
    session.flush()
    record_event(session, actor, "CUTOVER_INCIDENT_RAISED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "incident": inc["id"], "task": task_id, "severity": severity, "title": inc["title"], "escalated_to": path[0] if severity == "CRITICAL" else None})
    return inc


def _incident(r: CutoverRehearsal, incident_id: str) -> tuple[list[dict], dict]:
    incidents = [dict(i) for i in (r.incidents or [])]
    inc = next((i for i in incidents if i["id"] == incident_id), None)
    if inc is None:
        raise LookupError(f"incident {incident_id} not found")
    return incidents, inc


def escalate_incident(session: Session, r: CutoverRehearsal, incident_id: str, actor: str, note: str = "", to: str = "") -> dict:
    """One level up the path (or to a named person / role); the last level is the steering committee."""
    _live(r)
    incidents, inc = _incident(r, incident_id)
    if inc["status"] == "RESOLVED":
        raise ValueError("incident is resolved")
    path = inc.get("path") or DEFAULT_PATH
    if not to and inc["level"] >= len(path):
        raise ValueError(f"incident is already at the last escalation level ({path[-1]})")
    level = inc["level"] + 1
    target = to.strip()[:120] or path[level - 1]
    inc["escalations"] = list(inc.get("escalations") or []) + [{"level": level, "to": target, "by": actor, "at": _now().isoformat(), "note": note.strip()[:400]}]
    inc.update(status="ESCALATED", level=level)
    r.incidents = incidents
    _resummarise(r)
    session.flush()
    record_event(session, actor, "CUTOVER_INCIDENT_ESCALATED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "incident": incident_id, "level": level, "to": target})
    return inc


def resolve_incident(session: Session, r: CutoverRehearsal, incident_id: str, actor: str, resolution: str) -> dict:
    _live(r)
    incidents, inc = _incident(r, incident_id)
    if inc["status"] == "RESOLVED":
        raise ValueError("incident is already resolved")
    if not resolution.strip():
        raise ValueError("a resolution needs a note")
    inc.update(status="RESOLVED", resolved_by=actor, resolved_at=_now().isoformat(), resolution=resolution.strip()[:1000])
    r.incidents = incidents
    _resummarise(r)
    session.flush()
    record_event(session, actor, "CUTOVER_INCIDENT_RESOLVED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "incident": incident_id, "severity": inc["severity"]})
    return inc


# ----------------------------------------------------------------------------------------- assignments
def assign_task(session: Session, r: CutoverRehearsal, task_id: str, actor: str, assignee: str, backup: str = "", contact: str = "") -> dict:
    """Who runs a task during this rehearsal, with a backup and how to reach them (a name or role and a channel;
    never a credential). Empty assignee clears the assignment."""
    _live(r)
    _task(r, task_id)
    assignments = dict(r.assignments or {})
    if not assignee.strip():
        assignments.pop(task_id, None)
        entry = {}
    else:
        entry = {"assignee": assignee.strip()[:120], "backup": backup.strip()[:120], "contact": contact.strip()[:200], "by": actor, "at": _now().isoformat()}
        assignments[task_id] = entry
    r.assignments = assignments
    _resummarise(r)
    session.flush()
    record_event(session, actor, "CUTOVER_TASK_ASSIGNED", "MANIFEST", r.manifest_id, {"rehearsal_id": r.id, "task": task_id, "assignee": entry.get("assignee", "")})
    return entry


def assignment_table(r: CutoverRehearsal) -> list[dict]:
    a = r.assignments or {}
    return [{"task": t["id"], "name": t["name"], "phase": t["phase"], "owner": t["owner"], "downtime": t["downtime"], **{k: (a.get(t["id"]) or {}).get(k, "") for k in ("assignee", "backup", "contact")}} for t in r.runbook or []]


def manifest_of(session: Session, r: CutoverRehearsal) -> ScopeManifest | None:
    return session.get(ScopeManifest, r.manifest_id)

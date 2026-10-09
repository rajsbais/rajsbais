"""Orchestration (SIMULATED): job queue, pipelines, approved schedules, maintenance windows, target leases, retry, notifications.

The orchestrator adds NO authority. Every job calls an existing module method (delta run, selective execute, sweeps, agent run) that still
enforces its own approvals, content-hash bindings and separation of duties; jobs run under the `svc.scheduler` service principal, which
holds only run:execute and view. What the orchestrator adds is *when* and *in what order*:

  * maintenance windows per target system (allow rules incl. overnight wrap, blackouts that override them); no rules = unrestricted
  * target leases: one orchestrated writer per target; a full refresh program holds its target's lease from start to release/rollback
  * dependencies between jobs (pipelines), human gates, priorities, idempotent submission
  * retry with backoff for transient errors only; governance/configuration errors fail at once and are never retried; dead-lettering
  * schedules that need an approver (bound to the template hash) and never fire twice for one occurrence; missed windows are recorded, not replayed
  * an event feed plus subscriptions to external channels through a notifier adapter (only a recording stand-in exists)

NOT implemented: parallel workers (jobs run one at a time inside `tick`), a durable queue (state is in memory), a real clock-driven daemon
(an external scheduler must call `tick`), real e-mail/chat/webhook delivery.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..delta.engine import next_occurrence, validate_schedule
from ..sap.adapter import TransientError
from ..security.auth import DEMO_USERS, Forbidden, Principal, check_separation_of_duties
from ..service import Conflict, NotFound

PRIORITY = {"high": 0, "normal": 1, "low": 2}
SEVERITY = {"info": 0, "warn": 1, "error": 2}
TERMINAL = {"SUCCEEDED", "FAILED", "ATTENTION", "CANCELLED", "DEAD"}
WAITING = {"QUEUED", "WAITING_WINDOW", "WAITING_LEASE", "WAITING_DEPENDENCY", "WAITING_HUMAN", "RETRY_WAIT"}
RETRYABLE = (TransientError, TimeoutError, ConnectionError)
CHANNELS = {"email", "webhook", "chat"}
EVENT_KINDS = {"job.succeeded", "job.attention", "job.failed", "job.dead", "job.retry", "job.waiting_window", "job.waiting_lease",
               "job.cancelled", "gate.pending", "gate.escalated", "schedule.fired", "schedule.missed", "schedule.approved", "window.changed",
               "agent.findings", "notification.dropped"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


# kind -> (permission needed to submit, human-readable description)
KINDS = {
    "delta_run": ("run:execute", "Run an approved delta-refresh scenario (params: scenario_id)"),
    "selective_execute": ("run:execute", "Execute an approved selective-refresh project (params: project_id)"),
    "tdm_sweep": ("run:execute", "Release expired dataset reservations and expire datasets past retention"),
    "lean_sweep": ("run:execute", "Expire lean clients past their lease"),
    "agent_run": ("view", "Run a read-only AI agent (params: agent_id, params)"),
    "human_gate": ("plan:write", "Pause the pipeline until a human completes the gate (params: note, permission)"),
}


@dataclass
class Job:
    id: str
    kind: str
    params: dict
    priority: str
    submitted_by: str
    created: datetime
    target_id: str | None = None
    after: list[str] = field(default_factory=list)
    status: str = "QUEUED"
    attempts: int = 0
    max_attempts: int = 3
    next_attempt_at: datetime | None = None
    not_before: datetime | None = None
    started: datetime | None = None
    finished: datetime | None = None
    result: dict | None = None
    reasons: list[str] = field(default_factory=list)
    pipeline_id: str | None = None
    step: str | None = None
    schedule_id: str | None = None
    key: str | None = None  # idempotency key
    gate: dict | None = None
    events: list[dict] = field(default_factory=list)
    notified: set = field(default_factory=set)

    def log(self, now: datetime, msg: str, level: str = "info"):
        self.events.append({"ts": _iso(now), "level": level, "msg": msg})

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "params": self.params, "priority": self.priority, "submitted_by": self.submitted_by,
                "created": _iso(self.created), "target_id": self.target_id, "after": self.after, "status": self.status, "attempts": self.attempts,
                "max_attempts": self.max_attempts, "next_attempt_at": _iso(self.next_attempt_at), "not_before": _iso(self.not_before),
                "started": _iso(self.started), "finished": _iso(self.finished), "result": self.result, "reasons": self.reasons,
                "pipeline_id": self.pipeline_id, "step": self.step, "schedule_id": self.schedule_id, "gate": self.gate, "events": self.events[-30:],
                "simulated": True}


@dataclass
class Schedule:
    id: str
    name: str
    schedule: dict
    template: dict  # {"type": "job", "kind", "params", "priority"} | {"type": "pipeline", "steps": [...]}
    created_by: str
    created: datetime
    approval: dict | None = None
    paused: bool = False
    next_due: datetime | None = None
    fired: list[str] = field(default_factory=list)

    def hash(self) -> str:
        import hashlib
        import json
        return hashlib.sha256(json.dumps({"s": self.schedule, "t": self.template}, sort_keys=True, default=str).encode()).hexdigest()

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "schedule": self.schedule, "template": self.template, "created_by": self.created_by,
                "approval": self.approval, "approved": bool(self.approval and self.approval["hash"] == self.hash()), "paused": self.paused,
                "next_due": _iso(self.next_due), "fired": len(self.fired), "hash": self.hash()}


def next_due(schedule: dict, after: datetime) -> datetime:
    if schedule["kind"] == "daily":
        cand = after.replace(hour=schedule["hour"], minute=0, second=0, microsecond=0)
        return cand if cand > after else cand + timedelta(days=1)
    return next_occurrence(schedule, after)


def validate(s: dict) -> list[str]:
    if s.get("kind") == "daily":
        return [] if s.get("hour") in range(24) else ["daily schedule needs hour 0-23"]
    return validate_schedule(s)


class Notifier:
    """Delivery adapter. This stand-in records the message and 'delivers' it nowhere: no e-mail, chat or webhook is ever sent."""
    name = "recording-stand-in"

    def send(self, channel: str, destination: str, subject: str, body: str) -> None:
        return None


class OrchestrationService:
    def __init__(self, svc, notifier: Notifier | None = None):
        self.svc = svc
        self.notifier = notifier or Notifier()
        self.jobs: dict[str, Job] = {}
        self.schedules: dict[str, Schedule] = {}
        self.windows: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.events: list[dict] = []
        self.subs: dict[str, dict] = {}
        self.outbox: list[dict] = []
        self.skew = timedelta(0)  # SIMULATION: a movable clock so windows and schedules can be demonstrated
        self.pipelines: dict[str, dict] = {}
        self.runner = DEMO_USERS["svc.scheduler"]

    # ------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return _now() + self.skew

    @staticmethod
    def _human(actor: Principal, perm: str) -> None:
        if actor.kind == "agent":
            raise Forbidden("AI agents cannot use the orchestrator")
        if not actor.can(perm):
            raise Forbidden(f"{perm} required")

    @staticmethod
    def _strict_human(actor: Principal, perm: str) -> None:
        if actor.kind != "human":
            raise Forbidden(f"{actor.kind} principals cannot perform this step")
        if not actor.can(perm):
            raise Forbidden(f"{perm} required")

    def advance_clock(self, actor: Principal, hours: float) -> dict:
        self._strict_human(actor, "system:write")
        self.skew += timedelta(hours=hours)
        self.svc.audit.append(actor.id, "orchestration.clock_advanced", "orchestration", {"hours": hours, "simulated_now": _iso(self.now())})
        return {"simulated_now": _iso(self.now()), "note": "SIMULATION: a real scheduler uses the wall clock"}

    def get(self, jid: str) -> Job:
        if jid not in self.jobs:
            raise NotFound(f"job {jid}")
        return self.jobs[jid]

    # ------------------------------------------------------------ events and notifications
    def _event(self, kind: str, severity: str, subject: str, now: datetime, job: Job | None = None, **detail) -> dict:
        e = {"id": f"ev-{len(self.events) + 1}", "ts": _iso(now), "kind": kind, "severity": severity, "subject": subject,
             "job_id": job.id if job else None, "detail": detail}
        self.events.append(e)
        for s in self.subs.values():
            if s["active"] and kind in s["events"] and SEVERITY[severity] >= SEVERITY[s["min_severity"]]:
                self.outbox.append({"id": f"out-{len(self.outbox) + 1}", "event_id": e["id"], "subscription": s["id"], "channel": s["channel"],
                                    "destination": s["destination"], "subject": subject, "body": f"{kind} ({severity}); job {e['job_id'] or '-'}; see the control tower",
                                    "status": "PENDING", "attempts": 0})
        return e

    def _deliver(self) -> None:
        for o in self.outbox:
            if o["status"] != "PENDING":
                continue
            o["attempts"] += 1
            try:
                self.notifier.send(o["channel"], o["destination"], o["subject"], o["body"])
                o["status"] = "DELIVERED_SIMULATED" if self.notifier.name == "recording-stand-in" else "DELIVERED"
            except Exception as e:  # noqa: BLE001 - a broken channel must never stop the queue
                o["error"] = f"{type(e).__name__}: {e}"
                if o["attempts"] >= 3:
                    o["status"] = "DROPPED"
                    self.svc.audit.append("system", "orchestration.notification_dropped", o["id"], {"channel": o["channel"]})

    def subscribe(self, actor: Principal, spec: dict) -> dict:
        self._strict_human(actor, "system:write")
        ch = spec.get("channel")
        if ch not in CHANNELS:
            raise Conflict(f"channel must be one of {sorted(CHANNELS)}")
        if not spec.get("destination"):
            raise Conflict("destination is required")
        ev = set(spec.get("events") or EVENT_KINDS)
        if ev - EVENT_KINDS:
            raise Conflict(f"unknown event kind(s): {sorted(ev - EVENT_KINDS)}")
        sub = {"id": f"sub-{uuid.uuid4().hex[:6]}", "channel": ch, "destination": spec["destination"], "events": sorted(ev),
               "min_severity": spec.get("min_severity", "warn"), "active": True, "created_by": actor.id}
        if sub["min_severity"] not in SEVERITY:
            raise Conflict("min_severity must be info, warn or error")
        self.subs[sub["id"]] = sub
        self.svc.audit.append(actor.id, "orchestration.subscribed", sub["id"], {"channel": ch})
        return sub

    def unsubscribe(self, actor: Principal, sid: str) -> dict:
        self._strict_human(actor, "system:write")
        if sid not in self.subs:
            raise NotFound(f"subscription {sid}")
        self.subs[sid]["active"] = False
        return self.subs[sid]

    # ------------------------------------------------------------ maintenance windows
    def set_windows(self, actor: Principal, system_id: str, spec: dict) -> dict:
        self._strict_human(actor, "system:write")
        s = self.svc.system(system_id)
        if s.is_production:
            raise Conflict("production systems are never refresh targets: windows apply to targets only")
        allow = []
        for r in spec.get("allow", []):
            days = r.get("weekdays", list(range(7)))
            try:
                sh, sm = map(int, r["start"].split(":"))
                eh, em = map(int, r["end"].split(":"))
            except Exception:  # noqa: BLE001
                raise Conflict("allow rules need start and end as HH:MM")
            if not all(d in range(7) for d in days) or not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
                raise Conflict("invalid weekday or time in allow rule")
            if (sh, sm) == (eh, em):
                raise Conflict("an allow rule needs a non-empty interval")
            allow.append({"weekdays": sorted(days), "start": r["start"], "end": r["end"]})
        black = []
        for b in spec.get("blackouts", []):
            f, t = datetime.fromisoformat(b["from"]), datetime.fromisoformat(b["to"])
            f, t = (f if f.tzinfo else f.replace(tzinfo=timezone.utc)), (t if t.tzinfo else t.replace(tzinfo=timezone.utc))
            if t <= f:
                raise Conflict("blackout 'to' must be after 'from'")
            black.append({"from": _iso(f), "to": _iso(t), "reason": b.get("reason", "")})
        self.windows[system_id] = {"allow": allow, "blackouts": black, "set_by": actor.id}
        now = self.now()
        self._event("window.changed", "info", f"maintenance windows changed for {s.label}", now, system=system_id)
        self.svc.audit.append(actor.id, "orchestration.windows_set", system_id, {"allow": len(allow), "blackouts": len(black)})
        return self.window_state(system_id)

    @staticmethod
    def _in_allow(r: dict, t: datetime) -> bool:
        sh, sm = map(int, r["start"].split(":"))
        eh, em = map(int, r["end"].split(":"))
        mins, start, end = t.hour * 60 + t.minute, sh * 60 + sm, eh * 60 + em
        if start < end:
            return t.weekday() in r["weekdays"] and start <= mins < end
        # overnight: the window belongs to the weekday on which it STARTS
        if mins >= start:
            return t.weekday() in r["weekdays"]
        return ((t.weekday() - 1) % 7) in r["weekdays"] and mins < end

    def _open_at(self, system_id: str, t: datetime) -> tuple[bool, str]:
        w = self.windows.get(system_id)
        if not w or (not w["allow"] and not w["blackouts"]):
            return True, "no maintenance window configured (unrestricted)"
        for b in w["blackouts"]:
            if datetime.fromisoformat(b["from"]) <= t < datetime.fromisoformat(b["to"]):
                return False, f"blackout{': ' + b['reason'] if b['reason'] else ''}"
        if not w["allow"]:
            return True, "no allow rules; only blackouts apply"
        return (True, "inside an allowed window") if any(self._in_allow(r, t) for r in w["allow"]) else (False, "outside every allowed window")

    def window_state(self, system_id: str, now: datetime | None = None) -> dict:
        s = self.svc.system(system_id)
        now = now or self.now()
        ok, why = self._open_at(system_id, now)
        nxt = None
        if not ok:
            t = now.replace(second=0, microsecond=0)
            for _ in range(14 * 24 * 4):
                t += timedelta(minutes=15)
                if self._open_at(system_id, t)[0]:
                    nxt = t
                    break
        return {"system_id": system_id, "label": s.label, "open": ok, "reason": why, "next_open": _iso(nxt), "rules": self.windows.get(system_id),
                "now": _iso(now), "simulated_clock": bool(self.skew)}

    # ------------------------------------------------------------ leases
    def acquire(self, target_id: str, holder: str, kind: str) -> None:
        cur = self.leases.get(target_id)
        if cur and cur["holder"] != holder:
            raise Conflict(f"{self.svc.system(target_id).label} is leased by {cur['kind']} {cur['holder']}")
        self.leases[target_id] = {"holder": holder, "kind": kind, "since": _iso(self.now())}

    def release(self, holder: str) -> None:
        for t in [t for t, l in self.leases.items() if l["holder"] == holder]:
            del self.leases[t]

    # ------------------------------------------------------------ submission
    def _target_of(self, kind: str, params: dict) -> str | None:
        svc = self.svc
        if kind == "delta_run":
            return svc.delta.get(params["scenario_id"]).target_id
        if kind == "selective_execute":
            return svc.project(params["project_id"]).target_id
        return None

    def _make(self, actor: Principal, kind: str, params: dict, priority: str, after: list[str], now: datetime, **kw) -> Job:
        if kind not in KINDS:
            raise Conflict(f"unknown job kind {kind}; one of {sorted(KINDS)}")
        if priority not in PRIORITY:
            raise Conflict("priority must be high, normal or low")
        self._human(actor, KINDS[kind][0])
        try:
            target = self._target_of(kind, params)
        except KeyError as e:
            raise Conflict(f"missing or unknown parameter {e}")
        if kind == "agent_run" and params.get("agent_id") not in self.svc.agents.specs:
            raise Conflict("unknown agent_id")
        from ..security import authz
        authz.require_systems(self.svc, actor, target)
        if target and self.svc.system(target).is_production:
            raise Forbidden("production systems are never refresh targets")
        j = Job(f"job-{uuid.uuid4().hex[:8]}", kind, dict(params), priority, actor.id, now, target, list(after), **kw)
        if kind == "human_gate":
            j.gate = {"note": params.get("note", ""), "permission": params.get("permission", "plan:approve"), "completed_by": None}
            j.status = "WAITING_HUMAN" if not after else "WAITING_DEPENDENCY"
        self.jobs[j.id] = j
        return j

    def submit(self, actor: Principal, kind: str, params: dict | None = None, priority: str = "normal", after: list[str] | None = None,
               not_before: datetime | None = None, idempotency_key: str | None = None, max_attempts: int = 3) -> Job:
        if idempotency_key:
            for j in self.jobs.values():
                if j.key == idempotency_key:
                    return j
        for a in after or []:
            self.get(a)
        now = self.now()
        j = self._make(actor, kind, params or {}, priority, after or [], now, not_before=not_before, key=idempotency_key, max_attempts=max(1, min(max_attempts, 10)))
        self.svc.audit.append(actor.id, "orchestration.job_submitted", j.id, {"kind": kind, "target": j.target_id})
        return j

    def submit_pipeline(self, actor: Principal, spec: dict, schedule_id: str | None = None, now: datetime | None = None) -> dict:
        steps = spec.get("steps") or []
        if not steps:
            raise Conflict("a pipeline needs at least one step")
        keys = [s["key"] for s in steps]
        if len(set(keys)) != len(keys):
            raise Conflict("step keys must be unique")
        for s in steps:
            for a in s.get("after", []):
                if a not in keys:
                    raise Conflict(f"step {s['key']} depends on unknown step {a}")
        # cycle check (Kahn)
        indeg = {s["key"]: len(s.get("after", [])) for s in steps}
        ready = [k for k, d in indeg.items() if d == 0]
        seen = 0
        while ready:
            k = ready.pop()
            seen += 1
            for s in steps:
                if k in s.get("after", []):
                    indeg[s["key"]] -= 1
                    if indeg[s["key"]] == 0:
                        ready.append(s["key"])
        if seen != len(steps):
            raise Conflict("the pipeline contains a dependency cycle")
        now = now or self.now()
        pid = f"pl-{uuid.uuid4().hex[:8]}"
        made: dict[str, Job] = {}
        created: list[Job] = []
        try:
            for s in self._topo(steps):
                j = self._make(actor, s["kind"], s.get("params", {}), s.get("priority", "normal"), [made[a].id for a in s.get("after", [])], now,
                               pipeline_id=pid, step=s["key"], schedule_id=schedule_id, max_attempts=s.get("max_attempts", 3))
                made[s["key"]] = j
                created.append(j)
        except Exception:
            for j in created:  # all or nothing
                self.jobs.pop(j.id, None)
            raise
        self.pipelines[pid] = {"id": pid, "name": spec.get("name", pid), "jobs": [j.id for j in created], "submitted_by": actor.id, "created": _iso(now)}
        self.svc.audit.append(actor.id, "orchestration.pipeline_submitted", pid, {"jobs": len(created), "schedule": schedule_id})
        return self.pipeline(pid)

    @staticmethod
    def _topo(steps: list[dict]) -> list[dict]:
        out, done = [], set()
        while len(out) < len(steps):
            for s in steps:
                if s["key"] not in done and all(a in done for a in s.get("after", [])):
                    out.append(s)
                    done.add(s["key"])
        return out

    def pipeline(self, pid: str) -> dict:
        if pid not in self.pipelines:
            raise NotFound(f"pipeline {pid}")
        p = self.pipelines[pid]
        js = [self.jobs[j] for j in p["jobs"]]
        st = [j.status for j in js]
        status = ("SUCCEEDED" if all(s == "SUCCEEDED" for s in st) else "FAILED" if any(s in ("FAILED", "DEAD") for s in st)
                  else "ATTENTION" if any(s == "ATTENTION" for s in st) else "CANCELLED" if any(s == "CANCELLED" for s in st)
                  else "RUNNING")
        return {**p, "status": status, "jobs_detail": [j.public() for j in js]}

    def cancel(self, actor: Principal, jid: str) -> Job:
        self._strict_human(actor, "run:execute")
        j = self.get(jid)
        if j.status in TERMINAL:
            raise Conflict(f"job is {j.status}")
        if j.status == "RUNNING":
            raise Conflict("a running job cannot be cancelled")
        self._finish(j, "CANCELLED", self.now(), [f"cancelled by {actor.id}"])
        self.svc.audit.append(actor.id, "orchestration.job_cancelled", j.id, {})
        return j

    def complete_gate(self, actor: Principal, jid: str, note: str = "") -> Job:
        j = self.get(jid)
        if j.kind != "human_gate":
            raise Conflict("not a human gate")
        self._strict_human(actor, j.gate["permission"])
        if actor.id == j.submitted_by:
            raise Forbidden("separation of duties: the submitter cannot complete their own gate")
        if j.status != "WAITING_HUMAN":
            raise Conflict(f"gate is {j.status}")
        j.gate.update(completed_by=actor.id, note=note or j.gate["note"])
        self._finish(j, "SUCCEEDED", self.now(), [], result={"completed_by": actor.id})
        self.svc.audit.append(actor.id, "orchestration.gate_completed", j.id, {})
        return j

    # ------------------------------------------------------------ schedules
    def create_schedule(self, actor: Principal, spec: dict) -> Schedule:
        self._strict_human(actor, "run:execute")
        errs = validate(spec.get("schedule") or {})
        if errs or not spec.get("schedule"):
            raise Conflict("; ".join(errs) or "schedule is required")
        t = spec.get("template") or {}
        if t.get("type") == "job":
            if t.get("kind") not in KINDS or t["kind"] == "human_gate":
                raise Conflict("a job schedule needs a non-gate job kind")
        elif t.get("type") == "pipeline":
            if not t.get("steps"):
                raise Conflict("a pipeline template needs steps")
            for s in t["steps"]:
                if s.get("kind") not in KINDS:
                    raise Conflict(f"unknown job kind {s.get('kind')}")
        else:
            raise Conflict("template.type must be 'job' or 'pipeline'")
        s = Schedule(f"sch-{uuid.uuid4().hex[:8]}", spec.get("name", "schedule"), dict(spec["schedule"]), t, actor.id, self.now())
        self.schedules[s.id] = s
        self.svc.audit.append(actor.id, "orchestration.schedule_created", s.id, {"hash": s.hash()})
        return s

    def approve_schedule(self, actor: Principal, sid: str) -> Schedule:
        self._strict_human(actor, "plan:approve")
        s = self.schedules.get(sid) or (_ for _ in ()).throw(NotFound(f"schedule {sid}"))
        check_separation_of_duties(s.created_by, actor)
        now = self.now()
        s.approval = {"by": actor.id, "at": _iso(now), "hash": s.hash()}
        s.next_due = next_due(s.schedule, now)
        self._event("schedule.approved", "info", f"schedule {s.name} approved; first run {_iso(s.next_due)}", now)
        self.svc.audit.append(actor.id, "orchestration.schedule_approved", s.id, {"hash": s.hash()})
        return s

    def pause_schedule(self, actor: Principal, sid: str, paused: bool) -> Schedule:
        self._strict_human(actor, "run:execute")
        s = self.schedules.get(sid) or (_ for _ in ()).throw(NotFound(f"schedule {sid}"))
        s.paused = paused
        if not paused and s.approval:
            s.next_due = next_due(s.schedule, self.now())
        self.svc.audit.append(actor.id, "orchestration.schedule_paused", s.id, {"paused": paused})
        return s

    # ------------------------------------------------------------ the tick
    def tick(self, actor: Principal, now: datetime | None = None, max_jobs: int = 10) -> dict:
        """Called by an external scheduler. Fires due schedules, then dispatches ready jobs one at a time (no parallelism)."""
        self._human(actor, "run:execute")
        if actor.kind == "agent":
            raise Forbidden("AI agents cannot tick the orchestrator")
        now = now or self.now()
        out = {"fired": [], "missed": [], "ran": [], "waiting": [], "escalated": [], "now": _iso(now)}
        for s in self.schedules.values():
            if s.paused or not s.approval or not s.next_due or now < s.next_due:
                continue
            if s.approval["hash"] != s.hash():
                continue  # edited after approval: never fires
            due = s.next_due
            grace = timedelta(hours=s.schedule.get("window_hours", 4))
            s.next_due = next_due(s.schedule, max(now, due))
            if now > due + grace:
                self._event("schedule.missed", "warn", f"schedule {s.name} missed its occurrence at {_iso(due)}", now)
                self.svc.audit.append(actor.id, "orchestration.schedule_missed", s.id, {"due": _iso(due)})
                out["missed"].append({"schedule": s.id, "due": _iso(due)})
                continue
            fire_key = f"{s.id}@{_iso(due)}"
            if fire_key in s.fired:
                continue
            s.fired.append(fire_key)
            creator = DEMO_USERS.get(s.created_by) or self.runner
            try:
                if s.template["type"] == "job":
                    j = self._make(creator, s.template["kind"], s.template.get("params", {}), s.template.get("priority", "normal"), [], now,
                                   schedule_id=s.id, key=fire_key)
                    ids = [j.id]
                else:
                    ids = self.submit_pipeline(creator, {"name": s.name, "steps": s.template["steps"]}, schedule_id=s.id, now=now)["jobs"]
            except Exception as e:  # noqa: BLE001
                self._event("job.failed", "error", f"schedule {s.name} could not create its job: {e}", now)
                continue
            self._event("schedule.fired", "info", f"schedule {s.name} fired", now, jobs=ids)
            out["fired"].append({"schedule": s.id, "jobs": ids})
        ran = 0
        for j in sorted(self.jobs.values(), key=lambda x: (PRIORITY[x.priority], x.created)):
            if ran >= max_jobs:
                break
            if j.status in TERMINAL or j.status == "RUNNING":
                continue
            r = self._consider(j, now)
            if r == "ran":
                ran += 1
                out["ran"].append({"job": j.id, "status": j.status})
            elif r:
                out["waiting"].append({"job": j.id, "status": j.status, "why": r})
        for j in self.jobs.values():  # gate escalation
            if j.kind == "human_gate" and j.status == "WAITING_HUMAN" and "escalated" not in j.notified and \
                    now - j.created > timedelta(hours=float(j.params.get("escalate_after_hours", 8))):
                j.notified.add("escalated")
                self._event("gate.escalated", "warn", f"human gate {j.id} has waited more than {j.params.get('escalate_after_hours', 8)}h", now, j)
                out["escalated"].append(j.id)
        self._deliver()
        return out

    def _once(self, j: Job, kind: str, severity: str, subject: str, now: datetime, **d) -> None:
        if kind not in j.notified:
            j.notified.add(kind)
            self._event(kind, severity, subject, now, j, **d)

    def _consider(self, j: Job, now: datetime) -> str | None:
        """Advance one job if it is ready. Returns 'ran' if it executed, a reason string if it is waiting, None if nothing to say."""
        if j.not_before and now < j.not_before:
            return f"not before {_iso(j.not_before)}"
        if j.next_attempt_at and now < j.next_attempt_at:
            j.status = "RETRY_WAIT"
            return f"retry at {_iso(j.next_attempt_at)}"
        for d in j.after:
            dep = self.jobs[d]
            if dep.status in ("FAILED", "DEAD", "CANCELLED", "ATTENTION"):
                self._finish(j, "CANCELLED", now, [f"dependency {d} ended as {dep.status}"])
                return None
        if any(self.jobs[d].status != "SUCCEEDED" for d in j.after):
            j.status = "WAITING_DEPENDENCY"
            return "waiting for dependencies"
        if j.kind == "human_gate":
            j.status = "WAITING_HUMAN"
            self._once(j, "gate.pending", "warn", f"a human must complete gate {j.id}: {j.gate['note']}", now, permission=j.gate["permission"])
            return "waiting for a human"
        if j.target_id:
            st = self.window_state(j.target_id, now)
            if not st["open"]:
                j.status = "WAITING_WINDOW"
                self._once(j, "job.waiting_window", "info", f"{j.kind} waits for the maintenance window of {st['label']} ({st['reason']}); opens {st['next_open']}", now)
                return f"window closed: {st['reason']}"
            cur = self.leases.get(j.target_id)
            if cur and cur["holder"] != j.id:
                j.status = "WAITING_LEASE"
                self._once(j, "job.waiting_lease", "info", f"{j.kind} waits: {self.svc.system(j.target_id).label} is leased by {cur['kind']} {cur['holder']}", now)
                return f"target leased by {cur['kind']} {cur['holder']}"
            self.acquire(j.target_id, j.id, "job")
        try:
            self._run(j, now)
        finally:
            self.release(j.id)
        return "ran"

    # ------------------------------------------------------------ running a job
    def _run(self, j: Job, now: datetime) -> None:
        j.status, j.attempts, j.started, j.next_attempt_at = "RUNNING", j.attempts + 1, j.started or now, None
        j.log(now, f"attempt {j.attempts} started")
        svc, who = self.svc, self.runner
        try:
            if j.kind == "delta_run":
                rec = svc.delta.run(who, j.params["scenario_id"], trigger="schedule", now=now)
                ok = rec["status"] == "COMPLETED" and rec.get("release") != "HELD"
                self._finish(j, "SUCCEEDED" if ok else "ATTENTION", now, [] if ok else [f"delta run {rec['status']}, release {rec.get('release')}: an operator must look"],
                             result={k: rec.get(k) for k in ("version", "status", "release", "new", "changed", "loaded")})
            elif j.kind == "selective_execute":
                run = svc.execute(who, j.params["project_id"])
                ok = run.status == "COMPLETED" and run.release == "RELEASED"
                self._finish(j, "SUCCEEDED" if ok else "ATTENTION", now, [] if ok else [f"run {run.id} is {run.status}, release {run.release}"],
                             result={"run_id": run.id, "status": run.status, "release": run.release})
            elif j.kind == "tdm_sweep":
                self._finish(j, "SUCCEEDED", now, [], result=svc.tdm.sweep(who, now))
            elif j.kind == "lean_sweep":
                self._finish(j, "SUCCEEDED", now, [], result=svc.lean.sweep(who, now))
            elif j.kind == "agent_run":
                rep = svc.agents.run(who, j.params["agent_id"], j.params.get("params", {}))
                hi = [r for r in rep["recommendations"] if r["priority"] in ("critical", "high")]
                self._finish(j, "SUCCEEDED", now, [], result={"report_id": rep["id"], "summary": rep["summary"], "recommendations": len(rep["recommendations"]), "high_or_critical": len(hi)})
                if hi:
                    self._event("agent.findings", "warn", f"agent {rep['name']} produced {len(hi)} high-priority recommendation(s)", now, j, report=rep["id"])
        except RETRYABLE as e:
            if j.attempts >= j.max_attempts:
                self._finish(j, "DEAD", now, [f"transient error persisted after {j.attempts} attempts: {type(e).__name__}: {e}"])
            else:
                delay = min(3600, 60 * 2 ** (j.attempts - 1))
                j.status, j.next_attempt_at = "RETRY_WAIT", now + timedelta(seconds=delay)
                j.log(now, f"transient error, retry in {delay}s: {e}", "warn")
                self._event("job.retry", "info", f"{j.kind} will retry in {delay}s after: {e}", now, j, attempt=j.attempts)
        except (Forbidden, Conflict, NotFound, ValueError, KeyError) as e:
            # governance or configuration: a retry would be refused again, so fail now and say why
            self._finish(j, "FAILED", now, [f"{type(e).__name__}: {e}", "not retried: retrying a refused or invalid request cannot succeed"])

    def _finish(self, j: Job, status: str, now: datetime, reasons: list[str], result: dict | None = None) -> None:
        j.status, j.finished, j.reasons = status, now, reasons
        if result is not None:
            j.result = result
        j.log(now, f"{status}" + (f": {reasons[0]}" if reasons else ""), "info" if status == "SUCCEEDED" else "warn")
        kind, sev = {"SUCCEEDED": ("job.succeeded", "info"), "ATTENTION": ("job.attention", "warn"), "FAILED": ("job.failed", "error"),
                     "DEAD": ("job.dead", "error"), "CANCELLED": ("job.cancelled", "info")}[status]
        self._event(kind, sev, f"{j.kind} {j.id} {status.lower()}" + (f": {reasons[0]}" if reasons else ""), now, j)
        self.svc.audit.append("svc.scheduler" if status != "CANCELLED" else j.submitted_by, f"orchestration.job_{status.lower()}", j.id,
                              {"kind": j.kind, "attempts": j.attempts, "reasons": reasons[:2]})

    # ------------------------------------------------------------ views
    def summary(self) -> dict:
        c: dict[str, int] = {}
        for j in self.jobs.values():
            c[j.status] = c.get(j.status, 0) + 1
        return {"jobs": c, "schedules": len(self.schedules), "pipelines": len(self.pipelines), "leases": self.leases,
                "windows": {k: self.window_state(k) for k in self.windows}, "now": _iso(self.now()), "simulated_clock": bool(self.skew),
                "notifier": self.notifier.name, "limits": ["serial execution (no parallel workers)", "in-memory queue (not durable)",
                                                          "an external scheduler must call tick", "notifications are recorded, never sent"]}

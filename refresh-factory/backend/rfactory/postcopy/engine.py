"""Post-copy automation factory (Module 3).

Flow (maps to phases 5, 8, 9 and 12 of the full-refresh runbook):
  capture  a TARGET PROFILE of the target's own technical configuration BEFORE the system copy is taken (phase 5); it is
           validated (must not reference production), submitted and approved by someone other than its author
  assess   read-only: how much of production arrived in the target (per category)
  plan     ordered, version-compatible tasks; the approvals each changing task needs (basis lead / integration owner /
           security officer) - the run cannot start until every required label is approved by a different human holding that role
  execute  per task: pre-check -> snapshot -> action (retry on transient errors) -> post-check; a failed post-check restores the
           task's snapshot and halts the run; checkpoints allow resume; full run rollback restores every task in reverse
  gate     independent verification that nothing in the target still points at production and the target's own configuration
           is back (phase 12); evidence package with before/after per task

Guards: never on a production system, never on a system locked against modification, production references are never
(re)activated, AI agents cannot approve, the executor cannot approve its own run.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..sap.adapter import TransientError
from ..sap.techstate import CATEGORIES, LISTS, TechState, is_prod_ref, list_items, simulate_system_copy
from ..security.auth import Forbidden, Principal
from ..service import Conflict, NotFound
from .tasks import BY_ID, Ctx, ordered

LABELS = ("basis_lead", "integration_owner", "security_officer")
LIVE_JOB = ("scheduled", "released", "active")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Profile:
    id: str
    system_id: str
    name: str
    state: dict
    captured_by: str
    captured_at: str
    status: str = "DRAFT"  # DRAFT PENDING_APPROVAL APPROVED
    last_editor: str | None = None
    submitted_by: str | None = None
    approval: dict | None = None

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.state, sort_keys=True, default=str).encode()).hexdigest()

    def public(self) -> dict:
        return {"id": self.id, "system_id": self.system_id, "name": self.name, "status": self.status, "hash": self.hash(),
                "captured_by": self.captured_by, "captured_at": self.captured_at, "approval": self.approval,
                "summary": {c: (len(v) if isinstance(v, list) else (len(v) if isinstance(v, dict) else 1)) for c, v in self.state.items()}}


@dataclass
class PcRun:
    id: str
    target_id: str
    source_id: str | None
    profile_id: str
    created_by: str
    created: str
    mode: str
    tasks: list[dict]
    required_labels: list[str]
    approvals: dict = field(default_factory=dict)
    status: str = "PLANNED"  # PLANNED AWAITING_APPROVAL READY RUNNING HALTED COMPLETED ROLLED_BACK
    checkpoint: int = 0
    events: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    snapshots: dict = field(default_factory=dict)
    pre_digest: str = ""
    post_digest: str | None = None
    gate: dict | None = None
    halted_reason: str | None = None
    attempts: int = 0

    def log(self, level: str, msg: str, **kw):
        self.events.append({"ts": _now(), "level": level, "msg": msg, **kw})

    def public(self) -> dict:
        return {"id": self.id, "target_id": self.target_id, "source_id": self.source_id, "profile_id": self.profile_id, "created_by": self.created_by,
                "created": self.created, "mode": self.mode, "status": self.status, "tasks": self.tasks, "required_approvals": self.required_labels,
                "approvals": self.approvals, "checkpoint": self.checkpoint, "gate": self.gate, "halted_reason": self.halted_reason,
                "attempts": self.attempts, "events": self.events[-100:], "pre_digest": self.pre_digest, "post_digest": self.post_digest, "simulated": True}


def diff(before: dict, after: dict) -> list[str]:
    out: list[str] = []
    for cat in before:
        b, a = before[cat], after.get(cat)
        if b == a:
            continue
        if cat in LISTS:
            idf = LISTS[cat]
            bm, am = {i[idf]: i for i in b}, {i[idf]: i for i in a}
            for k in bm.keys() - am.keys():
                out.append(f"{cat}:{k} removed")
            for k in am.keys() - bm.keys():
                out.append(f"{cat}:{k} added")
            for k in am.keys() & bm.keys():
                for f in sorted(set(am[k]) | set(bm[k])):
                    if am[k].get(f) != bm[k].get(f):
                        out.append(f"{cat}:{k}.{f}: {bm[k].get(f)!r} -> {am[k].get(f)!r}")
        elif isinstance(b, dict):
            for f in sorted(set(b) | set(a or {})):
                if b.get(f) != (a or {}).get(f):
                    out.append(f"{cat}.{f}: {b.get(f)!r} -> {(a or {}).get(f)!r}")
        else:
            out.append(f"{cat}: {b!r} -> {a!r}")
    return out


class PostCopyService:
    def __init__(self, svc):
        self.svc = svc
        self.profiles: dict[str, Profile] = {}
        self.runs: dict[str, PcRun] = {}

    # ------------------------------------------------------------ helpers
    def tech(self, system_id: str) -> TechState:
        self.svc.system(system_id)
        self.svc.require_local(system_id, "post-copy automation")
        return self.svc.adapters[system_id].tech

    def prod_hosts(self) -> set[str]:
        out: set[str] = set()
        for s in self.svc.systems.values():
            if s.is_production and self.svc.is_local(s.id):  # a remote production system's hosts are unknown to the platform
                out |= self.svc.adapters[s.id].tech.hosts()
        return out

    def _guard_target(self, system_id: str):
        s = self.svc.system(system_id)
        if s.is_production:
            raise Forbidden("post-copy automation never runs on a production system")
        if not s.writable_target_allowed:
            raise Forbidden(f"{s.label} is locked against modification")
        return s

    # ------------------------------------------------------------ assessment (read-only)
    def assess(self, system_id: str, profile_id: str | None = None) -> dict:
        s, t, ph = self.svc.system(system_id), self.tech(system_id), self.prod_hosts()
        cats: dict[str, dict] = {}
        for cat in LISTS:
            items = list_items(t.s, cat)
            prod = [i for i in items if is_prod_ref(i, ph)]
            active = [i for i in prod if self._active(cat, i)]
            cats[cat] = {"items": len(items), "production_refs": len(prod), "active_production_refs": len(active),
                         "examples": [i.get(LISTS[cat]) for i in active][:5]}
        for cat in ("smtp", "tms", "hana"):
            v = t.s[cat]
            cats[cat] = {"items": 1, "production_refs": int(is_prod_ref(v, ph)), "active_production_refs": int(is_prod_ref(v, ph)), "examples": []}
        total = sum(c["active_production_refs"] for c in cats.values())
        out = {"system": s.label, "family": s.family, "categories": cats, "active_production_references": total,
               "logical_system": t.s["logical_system"], "licence_valid": t.s["license"]["valid"], "tms_consistent": t.s["tms"]["consistent"],
               "privileged_users": [u["user"] for u in t.s["users"] if "SAP_ALL" in u.get("roles", []) and not u.get("locked")], "simulated": True}
        if profile_id:
            p = self.get_profile(profile_id)
            ctx = Ctx(t, p.state, ph)
            out["tasks_needed"] = [{"id": tk.id, "name": tk.name, "approval": tk.approval, "violations": tk.violations(ctx)[:6]}
                                   for tk in ordered(None) if tk.compatible(s)[0] and tk.violations(ctx)]
        return out

    @staticmethod
    def _active(cat: str, item: dict) -> bool:
        if cat == "jobs":
            return item.get("status") in LIVE_JOB
        if cat == "users":
            return not item.get("locked")
        if cat == "sso_trusts":
            return bool(item.get("trusted"))
        return bool(item.get("active", True))

    # ------------------------------------------------------------ profiles (phase 5)
    def get_profile(self, pid: str) -> Profile:
        if pid not in self.profiles:
            raise NotFound(f"target profile {pid}")
        return self.profiles[pid]

    def capture_profile(self, actor: Principal, system_id: str, name: str) -> Profile:
        if not actor.can("plan:write"):
            raise Forbidden("plan:write required")
        from ..security import authz
        authz.require_systems(self.svc, actor, system_id)
        self._guard_target(system_id)
        t, ph = self.tech(system_id), self.prod_hosts()
        refs = [f"{c}:{i.get(LISTS[c])}" for c in LISTS for i in list_items(t.s, c) if is_prod_ref(i, ph)]
        refs += [c for c in ("smtp", "tms", "hana") if is_prod_ref(t.s[c], ph)]
        if refs:
            raise Conflict("the target currently references production, so it cannot be captured as a clean profile (capture it BEFORE the system copy): "
                           + ", ".join(refs[:8]))
        if not t.s["license"].get("valid"):
            raise Conflict("the target licence is not valid: nothing to restore from")
        p = Profile(f"tpf-{uuid.uuid4().hex[:8]}", system_id, name, t.snapshot(), actor.id, _now(), last_editor=actor.id)
        self.profiles[p.id] = p
        self.svc.audit.append(actor.id, "postcopy.profile.captured", p.id, {"system": self.svc.system(system_id).label, "hash": p.hash()})
        return p

    def submit_profile(self, actor: Principal, pid: str) -> Profile:
        p = self.get_profile(pid)
        if p.status != "DRAFT":
            raise Conflict(f"profile is {p.status}")
        p.status, p.submitted_by = "PENDING_APPROVAL", actor.id
        self.svc.audit.append(actor.id, "postcopy.profile.submitted", p.id, {})
        return p

    def approve_profile(self, actor: Principal, pid: str) -> Profile:
        if not actor.can("plan:approve") or actor.kind == "agent":
            raise Forbidden("plan:approve (human) required")
        p = self.get_profile(pid)
        if p.status != "PENDING_APPROVAL":
            raise Conflict(f"profile is {p.status}")
        if actor.id in {p.captured_by, p.last_editor, p.submitted_by}:
            raise Forbidden("separation of duties: the author of a profile cannot approve it")
        p.status, p.approval = "APPROVED", {"by": actor.id, "at": _now(), "hash": p.hash()}
        self.svc.audit.append(actor.id, "postcopy.profile.approved", p.id, p.approval)
        return p

    # ------------------------------------------------------------ runs
    def get_run(self, rid: str) -> PcRun:
        if rid not in self.runs:
            raise NotFound(f"post-copy run {rid}")
        return self.runs[rid]

    def create_run(self, actor: Principal, spec: dict) -> PcRun:
        if not actor.can("plan:write"):
            raise Forbidden("plan:write required")
        svc = self.svc
        from ..security import authz
        authz.require_systems(svc, actor, spec["target_id"], spec.get("source_id"))
        s = self._guard_target(spec["target_id"])
        p = self.get_profile(spec["profile_id"])
        if p.system_id != s.id:
            raise Conflict("the profile was captured from a different system")
        if p.status != "APPROVED" or p.approval["hash"] != p.hash():
            raise Conflict("the target profile must be approved")
        if spec.get("source_id"):
            svc.system(spec["source_id"])
        mode = spec.get("mode", "deactivate")
        if mode not in ("deactivate", "delete"):
            raise Conflict("mode must be deactivate or delete")
        try:
            plan = ordered(spec.get("tasks"))
        except KeyError as e:
            raise Conflict(str(e))
        ctx = Ctx(self.tech(s.id), p.state, self.prod_hosts(), mode=mode)
        tasks, labels = [], []
        for t in plan:
            ok, why = t.compatible(s)
            needs = ok and bool(t.violations(ctx))
            if needs and t.approval != "none" and t.approval not in labels:
                labels.append(t.approval)
            tasks.append({"id": t.id, "name": t.name, "approval": t.approval, "area": t.area, "status": "PENDING" if ok else "NOT_APPLICABLE",
                          "reason": None if ok else why, "needs_action": needs, "depends_on": list(t.depends_on)})
        run = PcRun(f"pcr-{uuid.uuid4().hex[:8]}", s.id, spec.get("source_id"), p.id, actor.id, _now(), mode, tasks, sorted(labels))
        run.pre_digest = self.tech(s.id).digest()
        run.status = "AWAITING_APPROVAL" if labels else "READY"
        self.runs[run.id] = run
        svc.audit.append(actor.id, "postcopy.run.created", run.id, {"target": s.label, "tasks": len(tasks), "approvals_needed": run.required_labels})
        return run

    def approve_run(self, actor: Principal, rid: str, label: str) -> PcRun:
        run = self.get_run(rid)
        if label not in LABELS:
            raise Conflict(f"label must be one of {LABELS}")
        if not actor.can(f"postcopy:approve:{label}") or actor.kind != "human":
            raise Forbidden(f"a human with the '{label}' role is required")
        if label not in run.required_labels:
            raise Conflict(f"this run needs no '{label}' approval")
        if actor.id == run.created_by:
            raise Forbidden("separation of duties: the person who planned the run cannot approve it")
        if run.status not in ("AWAITING_APPROVAL", "READY"):
            raise Conflict(f"run is {run.status}")
        run.approvals[label] = {"by": actor.id, "at": _now()}
        if set(run.required_labels) <= set(run.approvals):
            run.status = "READY"
        self.svc.audit.append(actor.id, "postcopy.run.approved", run.id, {"label": label})
        return run

    def _ctx(self, run: PcRun) -> Ctx:
        c = Ctx(self.tech(run.target_id), self.get_profile(run.profile_id).state, self.prod_hosts(), mode=run.mode)
        c.completed = {t["id"] for t in run.tasks if t["status"] in ("DONE", "ALREADY_COMPLIANT", "NOT_APPLICABLE")}
        return c

    def execute(self, actor: Principal, rid: str, fault_hook=None) -> PcRun:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        run = self.get_run(rid)
        if run.status not in ("READY", "HALTED"):
            raise Conflict(f"run is {run.status}" + ("; approvals are missing: " + ", ".join(sorted(set(run.required_labels) - set(run.approvals))) if run.status == "AWAITING_APPROVAL" else ""))
        s = self._guard_target(run.target_id)
        tech = self.tech(run.target_id)
        run.status, run.halted_reason, run.attempts = "RUNNING", None, run.attempts + 1
        self.svc.audit.append(actor.id, "postcopy.run.started", run.id, {"attempt": run.attempts})
        for t in run.tasks:
            if t["status"] in ("DONE", "ALREADY_COMPLIANT", "NOT_APPLICABLE"):
                continue
            task, ctx = BY_ID[t["id"]], self._ctx(run)
            ok, why = task.compatible(s)
            if not ok:
                t.update(status="NOT_APPLICABLE", reason=why); continue
            block = task.blocking(ctx)
            if block:
                return self._halt(run, t, "PRECHECK_FAILED", "; ".join(block), actor)
            before_viol = task.violations(ctx)
            if not before_viol:
                t.update(status="ALREADY_COMPLIANT", needs_action=False)
                run.evidence[t["id"]] = {"task": t["id"], "result": "already compliant", "changes": [], "digest": tech.digest(list(task.categories))}
                continue
            if task.approval != "none" and task.approval not in run.approvals:
                return self._halt(run, t, "APPROVAL_MISSING", f"task {t['id']} now needs a '{task.approval}' approval that was not collected at planning time", actor)
            snap = tech.snapshot(list(task.categories))
            run.snapshots[t["id"]] = snap
            try:
                self._with_retry(lambda: self._apply(task, ctx, fault_hook), run, t["id"])
            except Exception as e:
                tech.restore(snap)
                return self._halt(run, t, "FAILED", f"{type(e).__name__}: {e}; task state restored", actor)
            after_viol = task.violations(ctx)
            if after_viol:
                tech.restore(snap)
                return self._halt(run, t, "POSTCHECK_FAILED", "; ".join(after_viol[:5]) + "; task state restored", actor)
            after = tech.snapshot(list(task.categories))
            changes = diff(snap, after)
            ev = {"task": t["id"], "result": "changed", "before": snap, "after": after, "changes": changes, "violations_before": before_viol[:20],
                  "approval": run.approvals.get(task.approval), "digest": hashlib.sha256(json.dumps(after, sort_keys=True, default=str).encode()).hexdigest()}
            run.evidence[t["id"]] = ev
            t.update(status="DONE", changes=len(changes))
            run.checkpoint += 1
            run.log("info", f"{t['id']} done", changes=len(changes))
            self.svc.audit.append(actor.id, "postcopy.task.done", run.id, {"task": t["id"], "changes": len(changes), "violations_before": len(before_viol)})
        run.gate = self.gate(run.target_id, run.profile_id)
        run.post_digest = tech.digest()
        if run.gate["ok"]:
            run.status = "COMPLETED"
            run.log("info", "post-copy completed; gate passed")
        else:
            run.status, run.halted_reason = "HALTED", "verification gate failed: " + ", ".join(c["id"] for c in run.gate["checks"] if c["status"] == "fail")
        self.svc.audit.append(actor.id, "postcopy.run.finished", run.id, {"status": run.status, "gate": run.gate["ok"]})
        return run

    @staticmethod
    def _apply(task, ctx, hook):
        if hook:
            hook(task.id, "before_apply", ctx)
        task.apply(ctx)
        if hook:
            hook(task.id, "after_apply", ctx)

    def _with_retry(self, fn, run, what, attempts=3):
        for i in range(1, attempts + 1):
            try:
                return fn()
            except TransientError as e:
                run.log("warn", f"transient error in {what} (attempt {i}/{attempts}): {e}")
                if i == attempts:
                    raise

    def _halt(self, run: PcRun, t: dict, status: str, reason: str, actor: Principal) -> PcRun:
        t.update(status=status, reason=reason)
        run.status, run.halted_reason = "HALTED", f"{t['id']}: {reason}"
        run.log("error", run.halted_reason)
        self.svc.audit.append(actor.id, "postcopy.run.halted", run.id, {"task": t["id"], "status": status, "reason": reason[:300]})
        return run

    def resume(self, actor: Principal, rid: str, fault_hook=None) -> PcRun:
        run = self.get_run(rid)
        if run.status != "HALTED":
            raise Conflict("only HALTED runs can be resumed")
        for t in run.tasks:
            if t["status"] in ("FAILED", "PRECHECK_FAILED", "POSTCHECK_FAILED", "APPROVAL_MISSING"):
                t["status"] = "PENDING"
        self.svc.audit.append(actor.id, "postcopy.run.resumed", run.id, {})
        return self.execute(actor, rid, fault_hook)

    def rollback(self, actor: Principal, rid: str) -> PcRun:
        """Restore every task's captured snapshot in reverse order. Human basis lead only."""
        if not actor.can("postcopy:approve:basis_lead") or actor.kind != "human":
            raise Forbidden("rollback needs a human basis lead")
        run = self.get_run(rid)
        if run.status not in ("HALTED", "COMPLETED"):
            raise Conflict(f"run is {run.status}")
        self._guard_target(run.target_id)
        tech = self.tech(run.target_id)
        restored = 0
        for t in reversed(run.tasks):
            if t["status"] == "DONE" and t["id"] in run.snapshots:
                tech.restore(run.snapshots[t["id"]]); t["status"] = "ROLLED_BACK"; restored += 1
        run.status = "ROLLED_BACK"
        run.log("info", f"rolled back {restored} task(s)")
        run.post_digest = tech.digest()
        self.svc.audit.append(actor.id, "postcopy.run.rolled_back", run.id, {"tasks": restored})
        return run

    # ------------------------------------------------------------ verification gate (phase 12)
    def gate(self, system_id: str, profile_id: str | None = None) -> dict:
        s, t, ph = self.svc.system(system_id), self.tech(system_id), self.prod_hosts()
        checks = []

        def chk(cid, name, bad):
            checks.append({"id": cid, "name": name, "status": "fail" if bad else "pass", "detail": f"{len(bad)} problem(s)" if bad else "ok", "samples": bad[:6]})

        bad = []
        for cat in LISTS:
            for it in list_items(t.s, cat):
                if cat == "users":
                    continue
                if is_prod_ref(it, ph) and self._active(cat, it):
                    bad.append(f"{cat}:{it.get(LISTS[cat])}")
        for cat in ("smtp", "tms", "hana"):
            if is_prod_ref(t.s[cat], ph):
                bad.append(cat)
        bad += [f"params:{k}" for k, v in t.s["params"].items() if isinstance(v, str) and v in ph]
        chk("G1-NO-ACTIVE-PRODUCTION-REFERENCE", "Nothing active in the target points to a production system, host or tenant", bad)
        chk("G2-MAIL-SINK", "A test mail would reach a non-production relay", [t.s["smtp"]["relay_host"]] if is_prod_ref({"host": t.deliver_test_mail()}, ph) else [])
        chk("G3-PRIVILEGED-USERS", "No copied user holds SAP_ALL / SAP_NEW unlocked",
            [u["user"] for u in t.s["users"] if not u.get("locked") and u.get("roles") and set(u["roles"]) & {"SAP_ALL", "SAP_NEW"} and u.get("env") == "prod"])
        chk("G4-PRODUCTION-USERS-LOCKED", "No copied production user is unlocked", [u["user"] for u in t.s["users"] if u.get("env") == "prod" and not u.get("locked")])
        if profile_id:
            ctx = Ctx(t, self.get_profile(profile_id).state, ph)
            from .tasks import ordered
            for task in ordered(None):
                if task.compatible(s)[0]:
                    chk(f"T-{task.id}", f"{task.name}: target configuration complete", task.violations(ctx))
        return {"ok": all(c["status"] == "pass" for c in checks), "checks": checks, "system": s.label}

    # ------------------------------------------------------------ evidence
    def evidence_package(self, rid: str) -> bytes:
        run = self.get_run(rid)
        files = {"run.json": run.public(), "gate.json": run.gate or {}, "profile.json": self.get_profile(run.profile_id).public(),
                 "audit.json": self.svc.audit.entries(), "audit_verification.json": self.svc.audit.verify(),
                 "tasks.json": [{k: v for k, v in e.items() if k not in ("before", "after")} for e in run.evidence.values()]}
        for tid, e in run.evidence.items():
            files[f"tasks/{tid}.json"] = e
        buf, sums = io.BytesIO(), {}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, obj in files.items():
                data = json.dumps(obj, indent=2, default=str).encode()
                sums[name] = hashlib.sha256(data).hexdigest(); z.writestr(name, data)
            z.writestr("SHA256SUMS.json", json.dumps({"simulated": True, "files": sums}, indent=2))
        return buf.getvalue()

    # ------------------------------------------------------------ demo helper
    def simulate_copy(self, actor: Principal, source_id: str, target_id: str) -> dict:
        """SIMULATION: what a homogeneous system copy leaves in the target. Never touches a production target."""
        if not actor.can("system:write"):
            raise Forbidden("system:write required")
        self._guard_target(target_id)
        src = self.svc.system(source_id)
        r = simulate_system_copy(self.tech(source_id), self.tech(target_id))
        self.svc.audit.append(actor.id, "demo.system_copy_simulated", target_id, {"source": src.label})
        return {**r, "simulated": True, "assessment": self.assess(target_id)}

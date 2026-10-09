"""Full system refresh orchestration (SIMULATED).

Drives the 13-phase runbook end to end against simulated systems. The system copy itself (phase 7) is a *mechanism adapter*:
here an in-memory homogeneous copy; in production it would be SWPM / HANA backup-recovery / storage snapshot, which are NOT built.

Safety properties (each covered by tests):
  * target must be a non-production, unlocked system of the same product; the source is only ever read, through `ReadOnlyView`
  * the run needs two approvals bound to the program hash (change approver, Basis lead who confirms the backup); creator can approve neither
  * a pre-copy backup of the target (data + technical state) is taken once and verified by digest; rollback restores it exactly
  * the copy is atomic (built aside, swapped in) so a failed copy leaves the target untouched
  * from phase 7 until release the target holds UNMASKED production data and is flagged as such; only a verified masking pass clears it
  * masking covers every discovered sensitive field in the whole target (discovery-extended policy) and is verified by comparing against
    the pre-mask values; the pre-mask snapshot lives only inside that phase
  * post-copy (phases 8-9) runs through the post-copy factory with its own three approvals; phase 12 needs a security officer; phase 13 an approver
  * AI agents and service accounts can approve, sign off, release or execute nothing
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..masking.engine import MaskingEngine, MaskMode, Rule, discover_sensitive, template
from ..sap.adapter import ReadOnlyView
from ..sap.ddic import TABLES, key_of
from ..sap.techstate import simulate_system_copy
from ..security.auth import Forbidden, Principal, check_separation_of_duties
from ..service import Conflict, NotFound
from . import runbook

MECHANISMS = {"simulated-homogeneous-copy": "In-memory homogeneous copy (stand-in for SWPM / HANA backup-recovery / snapshot)"}
APPROVALS = {"change_approver": "plan:approve", "basis_lead": "postcopy:approve:basis_lead"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


class PhaseFailure(RuntimeError):
    """A phase did not meet its exit criteria: the program holds at that phase (nothing is skipped)."""


@dataclass
class Program:
    id: str
    name: str
    source_id: str
    target_id: str
    profile_id: str
    masking_policy_id: str
    mechanism: str
    backup_ref: str
    created_by: str
    created: str
    status: str = "DRAFT"  # DRAFT APPROVED RUNNING WAITING HELD FAILED AWAITING_RELEASE RELEASED ROLLED_BACK
    phases: list[dict] = field(default_factory=list)
    checkpoint: int = 2  # phases 1-2 are done at creation
    approvals: dict = field(default_factory=dict)
    signoff: dict | None = None
    released: dict | None = None
    waiting_for: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    # runtime state (never exposed)
    backup: dict | None = None
    quiesced: list[str] = field(default_factory=list)
    source_digest: str | None = None
    postcopy_run: str | None = None
    evidence: dict = field(default_factory=dict)
    unmasked_target: bool = False
    executed_by: str | None = None
    attempts: int = 0

    def log(self, level: str, msg: str, **kw):
        self.events.append({"ts": _now(), "level": level, "msg": msg, **kw})

    def hash(self) -> str:
        return _digest({"s": self.source_id, "t": self.target_id, "p": self.profile_id, "m": self.masking_policy_id,
                        "x": self.mechanism, "b": self.backup_ref})

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "source_id": self.source_id, "target_id": self.target_id, "profile_id": self.profile_id,
                "masking_policy_id": self.masking_policy_id, "mechanism": self.mechanism, "backup_ref": self.backup_ref,
                "created_by": self.created_by, "created": self.created, "status": self.status, "hash": self.hash(), "phases": self.phases,
                "checkpoint": self.checkpoint, "approvals": self.approvals, "signoff": self.signoff, "released": self.released,
                "waiting_for": self.waiting_for, "reasons": self.reasons, "validation": self.validation, "postcopy_run": self.postcopy_run,
                "unmasked_target": self.unmasked_target, "has_backup": self.backup is not None, "events": self.events[-150:],
                "attempts": self.attempts, "simulated": True,
                "mechanism_note": MECHANISMS.get(self.mechanism, "")}


class FullRefreshService:
    def __init__(self, svc):
        self.svc = svc
        self.programs: dict[str, Program] = {}

    # ------------------------------------------------------------ helpers
    def get(self, pid: str) -> Program:
        if pid not in self.programs:
            raise NotFound(f"full refresh {pid}")
        return self.programs[pid]

    @staticmethod
    def _human(actor: Principal, perm: str) -> None:
        if actor.kind != "human":
            raise Forbidden(f"{actor.kind} principals cannot perform this step")
        if not actor.can(perm):
            raise Forbidden(f"{perm} required")

    def unmasked_targets(self) -> list[str]:
        return [p.target_id for p in self.programs.values() if p.unmasked_target]

    def _phase(self, p: Program, no: int) -> dict:
        return p.phases[no - 1]

    def _set(self, p: Program, no: int, status: str, **detail) -> None:
        ph = self._phase(p, no)
        ph["status"] = status
        ph["detail"] = {**ph.get("detail", {}), **detail}
        if status == "DONE":
            ph["detail"].pop("waiting_for", None)
        if status in ("DONE", "FAILED", "HELD"):
            ph["finished"] = _now()

    # ------------------------------------------------------------ create (phases 1-2)
    def create(self, actor: Principal, spec: dict) -> Program:
        self._human(actor, "plan:write")
        svc = self.svc
        src, tgt = svc.system(spec["source_id"]), svc.system(spec["target_id"])
        v = runbook.validate_pair(src, tgt)
        if not v["ok"]:
            raise Conflict("; ".join(v["blockers"]))
        mech = spec.get("mechanism", "simulated-homogeneous-copy")
        if mech not in MECHANISMS:
            raise Conflict(f"unknown copy mechanism {mech}")
        pol_id = spec.get("masking_policy_id", "gdpr-standard")
        try:
            template(pol_id)
        except KeyError:
            raise Conflict("unknown masking policy")
        if not spec.get("backup_ref"):
            raise Conflict("backup_ref is required: name the backup/recovery plan the Basis lead will confirm")
        prof = svc.postcopy.get_profile(spec["profile_id"])
        if prof.system_id != tgt.id:
            raise Conflict("the profile was captured from a different system")
        if prof.status != "APPROVED" or prof.approval["hash"] != prof.hash():
            raise Conflict("an approved target profile captured BEFORE the copy is required (phase 5)")
        p = Program(f"fr-{uuid.uuid4().hex[:8]}", spec.get("name") or f"{src.sid} → {tgt.sid} full refresh", src.id, tgt.id, prof.id, pol_id, mech,
                    spec["backup_ref"], actor.id, _now(), validation=v)
        p.phases = [{"no": n, "name": name, "performed_by": who, "approval_required": ap, "rollback": rb, "status": "PENDING",
                     "started": None, "finished": None, "detail": {}} for n, name, who, ap, rb in runbook.PHASES]
        self._set(p, 1, "DONE", source=src.label, target=tgt.label)
        self._set(p, 2, "DONE", blockers=v["blockers"], warnings=v["warnings"])
        self.programs[p.id] = p
        svc.audit.append(actor.id, "fullrefresh.created", p.id, {"source": src.label, "target": tgt.label, "hash": p.hash()})
        return p

    # ------------------------------------------------------------ approvals (phases 3-4)
    def approve(self, actor: Principal, pid: str, label: str) -> Program:
        p = self.get(pid)
        if label not in APPROVALS:
            raise Conflict(f"label must be one of {sorted(APPROVALS)}")
        self._human(actor, APPROVALS[label])
        if label == "change_approver":
            check_separation_of_duties(p.created_by, actor)
        elif actor.id == p.created_by:
            raise Forbidden("separation of duties: the creator cannot confirm the backup plan")
        if p.status not in ("DRAFT", "APPROVED"):
            raise Conflict(f"program is {p.status}")
        if self.svc.system(p.target_id).is_production:
            raise Forbidden("production target")
        p.approvals[label] = {"by": actor.id, "at": _now(), "hash": p.hash()}
        if set(APPROVALS) <= set(p.approvals):
            p.status = "APPROVED"
        self.svc.audit.append(actor.id, "fullrefresh.approved", p.id, {"label": label, "hash": p.hash()})
        return p

    def _approvals_ok(self, p: Program) -> list[str]:
        return [l for l in APPROVALS if l not in p.approvals or p.approvals[l]["hash"] != p.hash()]

    # ------------------------------------------------------------ execution
    def run(self, actor: Principal, pid: str, fault_injector=None) -> Program:
        """Start or continue from the checkpoint until the program needs a human, fails, or reaches release."""
        self._human(actor, "run:execute")
        p = self.get(pid)
        if p.status in ("RELEASED", "ROLLED_BACK", "AWAITING_RELEASE"):
            raise Conflict(f"program is {p.status}")
        if p.status == "RUNNING":
            raise Conflict("program is already running")
        missing = self._approvals_ok(p)
        if missing:
            raise Conflict("approvals missing or stale: " + ", ".join(missing))
        svc = self.svc
        tgt = svc.system(p.target_id)
        if tgt.is_production or not tgt.writable_target_allowed:
            raise Forbidden("the target is production or locked against modification")
        if actor.id in (a["by"] for a in p.approvals.values()):
            raise Forbidden("separation of duties: an approver cannot execute the program")
        p.status, p.attempts, p.reasons, p.waiting_for, p.executed_by = "RUNNING", p.attempts + 1, [], [], actor.id
        svc.audit.append(actor.id, "fullrefresh.started", p.id, {"attempt": p.attempts, "from_phase": p.checkpoint + 1})
        while p.checkpoint < 13:
            no = p.checkpoint + 1
            ph = self._phase(p, no)
            ph["status"], ph["started"] = "RUNNING", ph["started"] or _now()
            try:
                if fault_injector:
                    fault_injector(no)
                outcome = getattr(self, f"_p{no}")(actor, p)
            except PhaseFailure as e:
                self._set(p, no, "HELD", reason=str(e))
                p.status, p.reasons = "HELD", [str(e)]
                p.log("warn", f"phase {no} held: {e}")
                svc.audit.append(actor.id, "fullrefresh.held", p.id, {"phase": no, "reason": str(e)[:300]})
                return p
            except Exception as e:  # noqa: BLE001 - infrastructure failure: resumable
                self._set(p, no, "FAILED", error=f"{type(e).__name__}: {e}")
                p.status, p.reasons = "FAILED", [f"phase {no}: {type(e).__name__}: {e}"]
                p.log("error", f"phase {no} failed: {e}")
                svc.audit.append(actor.id, "fullrefresh.failed", p.id, {"phase": no, "error": str(e)[:300]})
                return p
            if outcome and outcome.get("wait"):
                self._set(p, no, "WAITING", waiting_for=outcome["wait"])
                p.status, p.waiting_for = ("AWAITING_RELEASE" if no == 13 else "WAITING"), outcome["wait"]
                svc.audit.append(actor.id, "fullrefresh.waiting", p.id, {"phase": no, "for": outcome["wait"]})
                return p
            self._set(p, no, "DONE", **(outcome or {}))
            p.checkpoint = no
            p.log("info", f"phase {no} done: {ph['name']}")
        p.status = "RELEASED" if p.released else "AWAITING_RELEASE"
        svc.audit.append(actor.id, "fullrefresh.completed", p.id, {"phases": 13})
        return p

    # --- phase 3 / 4
    def _p3(self, actor, p):
        return {"approvals": {k: v["by"] for k, v in p.approvals.items() if k == "change_approver"}}

    def _p4(self, actor, p):
        if p.backup is None:
            a = self.svc.adapters[p.target_id]
            data = copy.deepcopy(a.data)
            p.backup = {"data": data, "tech": a.tech.snapshot(), "data_digest": _digest(data), "tech_digest": a.tech.digest(),
                        "owners": copy.deepcopy(a.owners), "outbound": copy.deepcopy(a._outbound)}
        a = self.svc.adapters[p.target_id]
        if p.backup["data_digest"] != _digest(a.data) and p.checkpoint < 4:
            raise PhaseFailure("the target changed after the backup was taken: restart the program with a fresh backup")
        return {"backup_ref": p.backup_ref, "data_digest": p.backup["data_digest"][:16], "tech_digest": p.backup["tech_digest"][:16],
                "confirmed_by": p.approvals["basis_lead"]["by"]}

    # --- phase 5
    def _p5(self, actor, p):
        prof = self.svc.postcopy.get_profile(p.profile_id)
        if prof.status != "APPROVED" or prof.approval["hash"] != prof.hash():
            raise PhaseFailure("the target profile is no longer approved")
        # the profile must describe the target as it is NOW: capturing after the copy would capture production
        tech = self.svc.adapters[p.target_id].tech
        ph = self.svc.postcopy.prod_hosts()
        from ..postcopy.tasks import Ctx, ordered
        ctx = Ctx(tech, prof.state, ph)
        drift = [t.id for t in ordered(None) if t.compatible(self.svc.system(p.target_id))[0] and t.violations(ctx)]
        if drift:
            raise PhaseFailure("the target no longer matches its approved profile before the copy: " + ", ".join(drift[:6]))
        return {"profile": prof.id, "profile_hash": prof.hash()[:16], "categories": len(prof.state)}

    # --- phase 6
    def _p6(self, actor, p):
        a = self.svc.adapters[p.target_id]
        jobs = [j["name"] for j in a.tech.s["jobs"] if j.get("status") in ("scheduled", "released")]
        ifs = [o for o in a.outbound_interfaces() if o.get("active")]
        for o in ifs:
            o["active"] = False
        p.quiesced = [o.get("name") for o in ifs]
        return {"jobs_to_suspend_in_real_system": len(jobs), "outbound_interfaces_deactivated": p.quiesced,
                "note": "simulation: scheduled jobs are replaced by the copy anyway; interfaces were switched off"}

    # --- phase 7
    def _p7(self, actor, p):
        svc = self.svc
        src_a, tgt_a = svc.adapters[p.source_id], svc.adapters[p.target_id]
        view = ReadOnlyView(src_a)
        before = _digest({t: view.select(t) for t in TABLES})
        p.source_digest = p.source_digest or before
        if before != p.source_digest:
            raise PhaseFailure("the source changed during the program (not expected for a quiesced snapshot)")
        new_data = {t: copy.deepcopy(view.select(t)) for t in TABLES}  # built aside: atomic swap
        # the mechanism may fail here without touching the target
        new_tech = copy.deepcopy(tgt_a.tech)
        res = simulate_system_copy(src_a.tech, new_tech)
        tgt_a.data = new_data
        tgt_a._idx.clear()
        tgt_a.tech.s = new_tech.s
        tgt_a.changelog, tgt_a.log_floor = [], 0
        p.unmasked_target = True
        rows = sum(len(r) for r in new_data.values())
        svc.audit.append(actor.id, "fullrefresh.copied", p.id, {"mechanism": p.mechanism, "rows": rows, "target_holds_unmasked_data": True})
        return {"mechanism": p.mechanism, "tables": len(new_data), "rows": rows, "technical": res, "unmasked_data_in_target": True}

    # --- phase 8 / 9
    def _p8(self, actor, p):
        pcs = self.svc.postcopy
        if p.postcopy_run is None:
            run = pcs.create_run(actor, {"target_id": p.target_id, "source_id": p.source_id, "profile_id": p.profile_id})
            p.postcopy_run = run.id
        run = pcs.get_run(p.postcopy_run)
        if run.status in ("AWAITING_APPROVAL", "PLANNED"):
            return {"wait": [f"post-copy approval: {l}" for l in run.required_labels if l not in run.approvals]}
        if run.status in ("READY", "HALTED"):
            run = pcs.execute(actor, run.id)
        if run.status != "COMPLETED":
            raise PhaseFailure(f"post-copy run {run.id} is {run.status}: {run.halted_reason or ''}")
        return {"postcopy_run": run.id, "tasks": len(run.tasks), "gate_ok": run.gate["ok"]}

    def _p9(self, actor, p):
        a = self.svc.postcopy.assess(p.target_id, p.profile_id)
        if a.get("tasks_needed") or a["active_production_references"]:
            raise PhaseFailure(f"configuration is not restored: {len(a.get('tasks_needed', []))} task(s) still need action, "
                               f"{a['active_production_references']} production reference(s)")
        return {"profile_restored": True, "tasks_needed": 0}

    # --- phase 10
    def _p10(self, actor, p):
        svc = self.svc
        src, tgt = svc.adapters[p.source_id], svc.adapters[p.target_id]
        checks: list[dict] = []

        def chk(cid, name, bad):
            checks.append({"id": cid, "name": name, "status": "fail" if bad else "pass", "detail": f"{len(bad)} problem(s)" if bad else "ok", "samples": bad[:5]})

        chk("S1-ROW-COUNTS", "Every table has the source row count", [f"{t}: {len(src.data[t])} vs {len(tgt.data[t])}" for t in TABLES if len(src.data[t]) != len(tgt.data[t])])
        dups = []
        for t in TABLES:
            keys = [key_of(t, r) for r in tgt.data[t]]
            if len(keys) != len(set(keys)):
                dups.append(t)
        chk("S2-NO-DUPLICATE-KEYS", "No duplicate keys in any table", dups)
        chk("S3-SOURCE-UNCHANGED", "The source was not modified", [] if _digest({t: src.data[t] for t in TABLES}) == p.source_digest else ["source digest changed"])
        gate = svc.postcopy.gate(p.target_id, p.profile_id)
        chk("S4-POSTCOPY-GATE", "Post-copy verification gate passes", [c["id"] for c in gate["checks"] if c["status"] == "fail"])
        t = tgt.tech.s
        chk("S5-LOGICAL-SYSTEM", "The target has its own logical system name", [] if t["logical_system"] != src.tech.s["logical_system"] else [t["logical_system"]])
        chk("S6-LICENCE-TMS", "Licence valid and transport domain consistent", [x for x, ok in (("licence", t["license"]["valid"]), ("tms", t["tms"]["consistent"])) if not ok])
        p.evidence["smoke"] = checks
        failed = [c["id"] for c in checks if c["status"] == "fail"]
        if failed:
            raise PhaseFailure("smoke tests failed: " + ", ".join(failed))
        return {"checks": len(checks), "passed": len(checks)}

    # --- phase 11
    def _p11(self, actor, p):
        svc = self.svc
        tgt = svc.adapters[p.target_id]
        pol = template(p.masking_policy_id)
        disc = discover_sensitive({t: tgt.data[t] for t in TABLES if tgt.data[t]})
        extra = [Rule(d["table"], d["field"], d["strategy"], d["category"], pol.rules[0].mode if pol.rules else MaskMode.PSEUDONYMIZE)
                 for d in disc if (d["table"], d["field"]) not in pol.fields()]
        pol.rules += extra
        errs = pol.validate()
        if errs:
            raise PhaseFailure("masking policy invalid: " + "; ".join(errs[:3]))
        eng = MaskingEngine(pol)
        masked: dict[str, list[dict]] = {}
        residual, changed = [], 0
        fields = pol.fields()
        for t in TABLES:
            rows = tgt.data[t]
            if not rows:
                masked[t] = rows
                continue
            out = [eng.mask_row(t, r) for r in rows]
            for f in {f for (tt, f) in fields if tt == t}:
                same = sum(1 for a, b in zip(rows, out) if isinstance(a.get(f), str) and a[f] != "" and a[f] == b.get(f))
                if same:
                    residual.append(f"{t}.{f}: {same}")
                changed += sum(1 for a, b in zip(rows, out) if a.get(f) != b.get(f))
            masked[t] = out
        if residual:
            raise PhaseFailure("masking left original values in place: " + ", ".join(residual[:5]))
        after = discover_sensitive({t: masked[t] for t in TABLES if masked[t]})
        uncovered = [f"{d['table']}.{d['field']}" for d in after if (d["table"], d["field"]) not in fields]
        if uncovered:
            raise PhaseFailure("sensitive fields without a rule: " + ", ".join(uncovered[:6]))
        tgt.data = masked  # atomic swap; the pre-mask rows were local to this phase
        tgt._idx.clear()
        if pol.rules and any(r.mode == MaskMode.ANONYMIZE for r in pol.rules):
            eng.destroy_ephemeral_key()
        p.unmasked_target = False
        stats = [{"table": t, "field": f, "rows": s["rows"], "masked": s["masked"]} for (t, f), s in sorted(eng.stats.items())]
        p.evidence["masking"] = {"policy": p.masking_policy_id, "rules": len(pol.rules), "discovery_extended": len(extra), "stats": stats}
        svc.audit.append(actor.id, "fullrefresh.masked", p.id, {"rules": len(pol.rules), "extended": len(extra), "values_changed": changed})
        return {"rules": len(pol.rules), "added_by_discovery": len(extra), "values_changed": changed, "residual_original_values": 0}

    # --- phase 12
    def _p12(self, actor, p):
        svc = self.svc
        gate = svc.postcopy.gate(p.target_id, p.profile_id)
        bad = [c["id"] for c in gate["checks"] if c["status"] == "fail"]
        if bad:
            raise PhaseFailure("security validation failed: " + ", ".join(bad[:6]))
        if p.unmasked_target:
            raise PhaseFailure("the target still holds unmasked data")
        if not p.signoff:
            return {"wait": ["security officer sign-off"]}
        return {"signed_off_by": p.signoff["by"]}

    def sign_off(self, actor: Principal, pid: str) -> Program:
        self._human(actor, "postcopy:approve:security_officer")
        p = self.get(pid)
        if actor.id in (p.created_by, p.executed_by):
            raise Forbidden("separation of duties: the creator or executor cannot sign off security")
        if p.status != "WAITING" or p.checkpoint != 11:
            raise Conflict("security sign-off is only possible once phases 1-11 are complete and the program waits at phase 12")
        p.signoff = {"by": actor.id, "at": _now()}
        self.svc.audit.append(actor.id, "fullrefresh.security_signoff", p.id, {})
        p.status, p.waiting_for = "APPROVED", []
        return p

    # --- phase 13
    def _p13(self, actor, p):
        return {"wait": ["release by the target owner / change approver"]} if p.released is None else {"released_by": p.released["by"]}

    def release(self, actor: Principal, pid: str) -> Program:
        self._human(actor, "plan:approve")
        p = self.get(pid)
        if p.status != "AWAITING_RELEASE":
            raise Conflict(f"program is {p.status}: only a program that completed phases 1-12 can be released")
        if actor.id in (p.created_by, p.executed_by):
            raise Forbidden("separation of duties: the creator or executor cannot release the environment")
        if p.unmasked_target:
            raise Forbidden("the target still holds unmasked data")
        gate = self.svc.postcopy.gate(p.target_id, p.profile_id)
        if not gate["ok"]:
            raise Conflict("the verification gate no longer passes: " + ", ".join(c["id"] for c in gate["checks"] if c["status"] == "fail"))
        p.released = {"by": actor.id, "at": _now()}
        self._set(p, 13, "DONE", released_by=actor.id)
        p.status, p.checkpoint, p.waiting_for = "RELEASED", 13, []
        p.backup = None  # the pre-refresh backup is no longer needed for rollback; a real backup follows its own retention
        self.svc.audit.append(actor.id, "fullrefresh.released", p.id, {"target": self.svc.system(p.target_id).label})
        return p

    # ------------------------------------------------------------ rollback
    def rollback(self, actor: Principal, pid: str) -> Program:
        self._human(actor, "run:execute")
        p = self.get(pid)
        if p.status in ("RELEASED", "ROLLED_BACK", "DRAFT", "APPROVED") and p.backup is None:
            raise Conflict(f"nothing to roll back ({p.status})")
        if p.status == "RUNNING":
            raise Conflict("program is running")
        if p.backup is None:
            raise Conflict("no verified backup exists (phase 4 has not completed)")
        a = self.svc.adapters[p.target_id]
        if self.svc.system(p.target_id).is_production:
            raise Forbidden("production target")
        a.data = copy.deepcopy(p.backup["data"])
        a._idx.clear()
        a.tech.s = {}
        a.tech.restore(p.backup["tech"])
        a.owners = copy.deepcopy(p.backup["owners"])
        a._outbound[:] = copy.deepcopy(p.backup["outbound"])
        if _digest(a.data) != p.backup["data_digest"] or a.tech.digest() != p.backup["tech_digest"]:
            raise RuntimeError("restore verification failed: the target does not match its backup")
        p.unmasked_target = False
        p.status, p.checkpoint, p.reasons, p.waiting_for = "ROLLED_BACK", 2, [], []
        self.svc.audit.append(actor.id, "fullrefresh.rolled_back", p.id, {"restored_digest": p.backup["data_digest"][:16]})
        p.log("info", "target restored from the pre-copy backup and verified by digest")
        return p

    # ------------------------------------------------------------ evidence
    def evidence_report(self, pid: str) -> dict:
        p = self.get(pid)
        return {"program": p.public(), "smoke": p.evidence.get("smoke"), "masking": p.evidence.get("masking"),
                "audit": self.svc.audit.entries(resource=p.id), "simulated": True,
                "note": "No data values are included: only counts, digests and field names."}

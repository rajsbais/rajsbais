"""Application service: projects, workflow state machine, governance gates and report/evidence generation."""
from __future__ import annotations

import copy
import io
import json
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from .agents import advisors
from .dependency.planner import Plan, Planner
from .dependency.registry import Registry
from .fullrefresh import runbook
from .masking.engine import MaskingEngine, MaskingPolicy, Rule, MaskMode, STRATEGIES, TEMPLATES, discover_sensitive, template
from .reconcile.validator import reconcile
from .sap.adapter import ProductionWriteBlocked, ReadOnlyView, SapSystem, SystemRole
from .sap.synthetic import SimulatedSap, make_demo_pair
from .security.audit import AuditLog
from .security.auth import Forbidden, Principal, check_separation_of_duties
from .selective.conflicts import Action, ConflictReport, analyze
from .selective.executor import Executor, Run
from .selective.manifest import Manifest, Scope


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    """Workflow precondition not met (HTTP 409)."""


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Project:
    id: str
    name: str
    source_id: str
    target_id: str
    created_by: str
    status: str = "DRAFT"  # DRAFT PLANNED CONFLICTS_ANALYZED PENDING_APPROVAL APPROVED RUNNING COMPLETED HELD FAILED ROLLED_BACK
    manifests: list[Manifest] = field(default_factory=list)
    plan: Plan | None = None
    report: ConflictReport | None = None
    masking_policy: MaskingPolicy | None = None
    approval: dict | None = None
    submitted_by: str | None = None
    last_editor: str | None = None
    runs: list[str] = field(default_factory=list)
    created: str = field(default_factory=_now)

    @property
    def manifest(self) -> Manifest | None:
        return self.manifests[-1] if self.manifests else None


class RefreshService:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or Path(tempfile.mkdtemp(prefix="rfactory-"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.data_dir / "audit.jsonl")
        self.registries = {"ECC": Registry("ECC"), "S4": Registry("S4")}
        self.registry = self.registries["ECC"]  # default / ECC
        self.systems: dict[str, SapSystem] = {}
        self.adapters: dict[str, SimulatedSap] = {}
        self.projects: dict[str, Project] = {}
        self.runs: dict[str, Run] = {}
        self.executor = Executor(self.registry, self.data_dir / "staging", self.audit)
        self.engines: dict[str, MaskingEngine] = {}
        self.required_sensitive: dict[str, list[dict]] = {}
        from .delta.engine import DeltaService  # lazy: delta imports this module
        self.delta = DeltaService(self)
        from .tdm.engine import TdmService
        self.tdm = TdmService(self)
        from .leanclient.engine import LeanClientService
        self.lean = LeanClientService(self)
        from .postcopy.engine import PostCopyService
        self.postcopy = PostCopyService(self)
        from .agents.service import AgentService
        self.agents = AgentService(self)
        from .fullrefresh.engine import FullRefreshService
        self.full = FullRefreshService(self)

    # ---------------- landscape ----------------
    def register_system(self, actor: Principal, system: SapSystem, adapter: SimulatedSap | None = None) -> SapSystem:
        system.id = system.id or f"sys-{uuid.uuid4().hex[:8]}"
        if adapter is None:
            raise Conflict("only simulated adapters exist in the MVP: pass a SimulatedSap")
        adapter.system = system
        self.systems[system.id] = system
        self.adapters[system.id] = adapter
        self.audit.append(actor.id, "system.registered", system.id, {"label": system.label, "adapter": system.adapter})
        return system

    def bootstrap_demo(self, actor: Principal) -> dict:
        src, tgt = make_demo_pair()
        s = self.register_system(actor, src.system, src)
        t = self.register_system(actor, tgt.system, tgt)
        s4s, s4t = make_demo_pair("S4")
        a = self.register_system(actor, s4s.system, s4s)
        b = self.register_system(actor, s4t.system, s4t)
        return {"source": s.model_dump(mode="json"), "target": t.model_dump(mode="json"),
                "s4_source": a.model_dump(mode="json"), "s4_target": b.model_dump(mode="json"), "simulated": True}

    def reg(self, p: Project) -> Registry:
        r = self.registries[self.system(p.source_id).family]
        self.executor.reg = r
        return r

    def system(self, sid: str) -> SapSystem:
        if sid not in self.systems:
            raise NotFound(f"system {sid}")
        return self.systems[sid]

    def source_view(self, sid: str) -> ReadOnlyView:
        self.system(sid)
        return ReadOnlyView(self.adapters[sid])

    def discover(self, sid: str) -> dict:
        d = self.adapters[self.system(sid).id].discover()
        cust = [r for r in self.adapters[sid].data["KNA1"]]
        d["business_objects"] = {
            "customers": len(cust), "vendors": self.adapters[sid].count("LFA1"), "materials": self.adapters[sid].count("MARA"),
            "sales_orders": self.adapters[sid].count("VBAK"), "deliveries": self.adapters[sid].count("LIKP"),
            "billing_documents": self.adapters[sid].count("VBRK"), "accounting_documents": self.adapters[sid].count("BKPF"),
            "purchase_orders": self.adapters[sid].count("EKKO")}
        return d

    def readiness(self, sid: str) -> dict:
        s, a = self.system(sid), self.adapters[sid]
        checks = [
            {"id": "R1", "name": "Connectivity (simulated adapter)", "ok": True},
            {"id": "R2", "name": "Read-only discovery", "ok": True},
            {"id": "R3", "name": "Owner registered", "ok": bool(s.owner)},
        ]
        if not s.is_production:
            checks += [
                {"id": "T1", "name": "Allowed as write target", "ok": s.can_be_write_target},
                {"id": "T2", "name": "Outbound interfaces inactive", "ok": not any(o.get("active") for o in a.outbound_interfaces())},
                {"id": "T3", "name": "Number range intervals present", "ok": a.number_level("SD_ORDER") is not None},
            ]
        else:
            checks.append({"id": "S1", "name": "Production: extraction only, writes structurally blocked", "ok": True})
        return {"system": s.label, "ready": all(c["ok"] for c in checks), "checks": checks, "simulated": True}

    def refresh_combinations(self) -> list[dict]:
        out = []
        for s in self.systems.values():
            for t in self.systems.values():
                if s.id == t.id:
                    continue
                v = runbook.validate_pair(s, t)
                out.append({"source": s.id, "target": t.id, "label": f"{s.label} → {t.label}",
                            "selective": v["ok"] or (not t.is_production and s.product == t.product),
                            "note": "ECC↔S/4HANA is a migration/conversion, not a refresh" if s.family != t.family else "",
                            "full_system_refresh": v["ok"], "blockers": v["blockers"], "warnings": v["warnings"]})
        return out

    # ---------------- projects ----------------
    def project(self, pid: str) -> Project:
        if pid not in self.projects:
            raise NotFound(f"project {pid}")
        return self.projects[pid]

    def create_project(self, actor: Principal, name: str, source_id: str, target_id: str) -> Project:
        s, t = self.system(source_id), self.system(target_id)
        if t.is_production:
            raise Forbidden("production systems cannot be selected as refresh targets")
        if not t.can_be_write_target:
            raise Forbidden(f"{t.label} is locked against being overwritten")
        if s.id == t.id:
            raise Conflict("source and target must differ")
        if s.product != t.product:
            raise Conflict(f"product mismatch: {s.product} vs {t.product}"
                           + (" (ECC↔S/4HANA is a migration, not a refresh)" if s.family != t.family else ""))
        p = Project(f"prj-{uuid.uuid4().hex[:8]}", name, source_id, target_id, actor.id)
        self.projects[p.id] = p
        self.audit.append(actor.id, "project.created", p.id, {"source": s.label, "target": t.label})
        return p

    def _reset_downstream(self, p: Project):
        p.plan, p.report, p.approval, p.submitted_by = None, None, None, None
        p.status = "DRAFT"

    def set_manifest(self, actor: Principal, pid: str, scope: Scope, include_downstream: list[str],
                     masking_policy_id: str, conflict_policy: dict[str, str], instance_overrides: dict[str, str] | None = None,
                     approved_exceptions: dict[str, str] | None = None, editor: Principal | None = None) -> Manifest:
        p = self.project(pid)
        if p.status in ("RUNNING",):
            raise Conflict("project is running")
        prev = p.manifest
        m = Manifest(name=p.name, version=(prev.version + 1) if prev else 1, source_system_id=p.source_id,
                     target_system_id=p.target_id, scope=scope, include_downstream=include_downstream,
                     masking_policy_id=masking_policy_id, conflict_policy=conflict_policy,
                     instance_overrides=instance_overrides or {},
                     approved_exceptions=dict(approved_exceptions if approved_exceptions is not None
                                              else (prev.approved_exceptions if prev else {})))
        p.manifests.append(m)
        self._reset_downstream(p)
        p.last_editor = (editor or actor).id
        if masking_policy_id in TEMPLATES and (p.masking_policy is None or p.masking_policy.id != masking_policy_id):
            p.masking_policy = template(masking_policy_id)
        self.audit.append(actor.id, "manifest.created", pid, {"version": m.version, "hash": m.content_hash()})
        return m

    def build_plan(self, actor: Principal, pid: str) -> dict:
        p = self.project(pid)
        if not p.manifest:
            raise Conflict("no manifest")
        plan = Planner(self.source_view(p.source_id), self.reg(p)).build(p.manifest)
        p.plan, p.report, p.approval = plan, None, None
        p.status = "PLANNED"
        rows = {t: [r for i in plan.instances.values() for r in i.rows.get(t, [])] for t in
                {t for i in plan.instances.values() for t in i.rows}}
        self.required_sensitive[pid] = discover_sensitive(rows)
        self.audit.append(actor.id, "plan.built", pid, {"hash": plan.manifest_hash, "instances": len(plan.instances),
                                                         "blocking": len(plan.blocking)})
        return self.plan_summary(pid)

    def plan_summary(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        s = p.plan.summary()
        s["estimate"] = advisors.estimate_duration(bytes_total=s["bytes"], rows_total=s["total_rows"])
        s["simulated"] = True
        s["source_total_rows"] = sum(self.adapters[p.source_id].table_counts().values())
        return s

    def instances(self, pid: str, type_: str | None, origin: str | None, offset: int, limit: int) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        items = [i for i in p.plan.order if (not type_ or p.plan.instances[i].type == type_)
                 and (not origin or p.plan.instances[i].origin == origin)]
        page = [{"id": i, "type": p.plan.instances[i].type, "origin": p.plan.instances[i].origin,
                 "parent": p.plan.instances[i].parent, "requires": p.plan.instances[i].requires,
                 "configs": p.plan.instances[i].configs, "rows": p.plan.instances[i].row_counts(),
                 "decision": p.report.decisions[i].value if p.report and i in p.report.decisions else None}
                for i in items[offset:offset + limit]]
        return {"total": len(items), "offset": offset, "items": page}

    # ---------------- masking ----------------
    def masking_state(self, pid: str) -> dict:
        p = self.project(pid)
        disc = self.required_sensitive.get(pid, [])
        pol = p.masking_policy
        cov = [{**d, "covered": bool(pol and (d["table"], d["field"]) in pol.fields())} for d in disc]
        return {"policy": pol.to_dict() if pol else None, "templates": TEMPLATES, "strategies": sorted(STRATEGIES),
                "discovered": cov, "uncovered": sum(1 for c in cov if not c["covered"])}

    def add_masking_rules(self, actor: Principal, pid: str, rules: list[dict]) -> dict:
        p = self.project(pid)
        if p.status == "RUNNING":
            raise Conflict("project is running")
        p.masking_policy = p.masking_policy or MaskingPolicy("custom", "Custom policy")
        for r in rules:
            p.masking_policy.rules = [x for x in p.masking_policy.rules if (x.table, x.field) != (r["table"], r["field"])]
            p.masking_policy.rules.append(Rule(r["table"], r["field"], r["strategy"], r.get("category", r["strategy"].lower()),
                                               MaskMode(r.get("mode", "PSEUDONYMIZE"))))
        errs = p.masking_policy.validate()
        if errs:
            raise Conflict("; ".join(errs))
        p.report, p.approval = None, None
        p.status = "PLANNED" if p.plan else "DRAFT"
        self.audit.append(actor.id, "masking.rules_updated", pid, {"rules": len(rules)})
        return self.masking_state(pid)

    # ---------------- conflicts ----------------
    def analyze_conflicts(self, actor: Principal, pid: str) -> dict:
        p = self.project(pid)
        if not p.plan:
            raise Conflict("plan not built")
        sens = self._sensitive(p)
        p.report = analyze(p.plan, self.adapters[p.target_id], p.manifest, self.reg(p), sens)
        p.approval = None
        p.status = "CONFLICTS_ANALYZED"
        self.audit.append(actor.id, "conflicts.analyzed", pid, {"findings": len(p.report.findings), "blocking": len(p.report.blocking)})
        return self.conflicts(pid)

    def _sensitive(self, p: Project) -> set[tuple[str, str]]:
        """Fields whose values legitimately differ source-vs-target (masked or about to be)."""
        return (p.masking_policy.fields() if p.masking_policy else set()) | {
            (d["table"], d["field"]) for d in self.required_sensitive.get(p.id, [])}

    def conflicts(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.report:
            raise Conflict("conflicts not analyzed")
        d = p.report.to_dict()
        d["simulated"] = True
        return d

    def suggest_policies(self, pid: str) -> dict:
        p = self.project(pid)
        if not p.report or not p.plan:
            raise Conflict("conflicts not analyzed")
        sens = self._sensitive(p)
        alts = []
        for name, pol in (("current", dict(p.manifest.conflict_policy)),
                          ("skip-differences", {**p.manifest.conflict_policy, "DUPLICATE_DIFFERENT": "SKIP"}),
                          ("quarantine-differences", {**p.manifest.conflict_policy, "DUPLICATE_DIFFERENT": "QUARANTINE"})):
            m = p.manifest.model_copy(update={"conflict_policy": pol})
            r = analyze(p.plan, self.adapters[p.target_id], m, self.reg(p), sens)
            alts.append({"policy": name, "settings": pol, "blocking": bool(r.blocking), "executable_objects": len(r.executable),
                         "quarantined": sum(1 for a in r.decisions.values() if a == Action.QUARANTINE)})
        return advisors.conflict_resolution(p.report.to_dict(), alts)

    def apply_conflict_policy(self, actor: Principal, pid: str, policy: dict[str, str],
                              overrides: dict[str, str] | None = None) -> Manifest:
        p = self.project(pid)
        m = p.manifest
        return self.set_manifest(actor, pid, m.scope, m.include_downstream, m.masking_policy_id,
                                 {**m.conflict_policy, **policy}, {**m.instance_overrides, **(overrides or {})})

    def approve_exception(self, actor: Principal, pid: str, instance: str, justification: str) -> Manifest:
        if not actor.can("exception:approve"):
            raise Forbidden("exception:approve required")
        if actor.kind == "agent":
            raise Forbidden("AI agents may not approve exceptions")
        if not justification.strip():
            raise Conflict("justification required")
        p = self.project(pid)
        if p.last_editor == actor.id:
            raise Forbidden("separation of duties: editor cannot approve their own exception")
        m = p.manifest
        # the exception approver is not the editor of the manifest: keep the previous editor for separation of duties
        editor = Principal(p.last_editor or actor.id, "", ())
        new = self.set_manifest(actor, pid, m.scope, m.include_downstream, m.masking_policy_id, m.conflict_policy,
                                m.instance_overrides, {**m.approved_exceptions, instance: justification}, editor)
        self.audit.append(actor.id, "exception.approved", pid, {"instance": instance, "justification": justification})
        return new

    # ---------------- approval ----------------
    def submit(self, actor: Principal, pid: str) -> Project:
        p = self.project(pid)
        if not (p.plan and p.report):
            raise Conflict("plan and conflict analysis are required")
        if p.plan.blocking:
            raise Conflict(f"plan has {len(p.plan.blocking)} blocking dependency issue(s)")
        if p.report.blocking:
            raise Conflict(f"{len(p.report.blocking)} blocking target conflict(s) must be resolved by policy or exception")
        if not p.masking_policy:
            raise Conflict("a masking policy is required (privacy-safe by default)")
        cov = MaskingEngine(p.masking_policy).coverage(self.required_sensitive.get(pid, []))
        if not cov["complete"]:
            raise Conflict(f"{len(cov['missing'])} discovered sensitive field(s) have no masking rule")
        p.status, p.submitted_by = "PENDING_APPROVAL", actor.id
        self.audit.append(actor.id, "plan.submitted", pid, {"hash": p.plan.manifest_hash})
        return p

    def approve(self, actor: Principal, pid: str, comment: str = "") -> Project:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        p = self.project(pid)
        if p.status != "PENDING_APPROVAL":
            raise Conflict(f"project is {p.status}, not PENDING_APPROVAL")
        for creator in {p.created_by, p.last_editor, p.submitted_by} - {None}:
            check_separation_of_duties(creator, actor)
        p.approval = {"by": actor.id, "at": _now(), "manifest_hash": p.plan.manifest_hash, "comment": comment}
        p.status = "APPROVED"
        self.audit.append(actor.id, "plan.approved", pid, p.approval)
        return p

    def reject(self, actor: Principal, pid: str, comment: str) -> Project:
        if not actor.can("plan:approve"):
            raise Forbidden("plan:approve required")
        p = self.project(pid)
        p.status, p.approval = "DRAFT", None
        self.audit.append(actor.id, "plan.rejected", pid, {"comment": comment})
        return p

    # ---------------- execution ----------------
    def execute(self, actor: Principal, pid: str, fault_injector=None) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        p = self.project(pid)
        if p.status != "APPROVED" or not p.approval:
            raise Conflict("project is not approved")
        if p.approval["manifest_hash"] != p.manifest.content_hash() or p.plan.manifest_hash != p.manifest.content_hash():
            raise Conflict("approved plan does not match the current manifest; re-plan and re-approve")
        target = self.adapters[p.target_id]
        if target.system.is_production:
            raise ProductionWriteBlocked("production systems are never writable")
        engine = MaskingEngine(p.masking_policy)
        self.engines[pid] = engine
        self.reg(p)
        run = self.executor.new_run(pid, p.plan, p.report)
        self.runs[run.id] = run
        p.runs.append(run.id)
        p.status = "RUNNING"
        self.audit.append(actor.id, "run.started", run.id, {"project": pid, "objects": len(run.order)})
        target.fault_injector = fault_injector
        try:
            self.executor.execute(run, p.plan, p.report, engine, target, actor.id)
        finally:
            target.fault_injector = None
        return self._after_run(p, run)

    def resume(self, actor: Principal, run_id: str, fault_injector=None) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        run = self.run(run_id)
        p = self.project(run.project_id)
        if run.status != "FAILED":
            raise Conflict("only FAILED runs can be resumed")
        target = self.adapters[p.target_id]
        self.reg(p)
        target.fault_injector = fault_injector
        self.audit.append(actor.id, "run.resumed", run.id, {"checkpoint": run.checkpoint})
        try:
            self.executor.execute(run, p.plan, p.report, self.engines[p.id], target, actor.id)
        finally:
            target.fault_injector = None
        return self._after_run(p, run)

    def _after_run(self, p: Project, run: Run) -> Run:
        if run.status == "COMPLETED":
            run.reconciliation = reconcile(run, p.plan, self.source_view(p.source_id), self.adapters[p.target_id],
                                           self.engines[p.id], self.reg(p), self.required_sensitive.get(p.id, []),
                                           p.report.row_exclusions)
            run.release = run.reconciliation["release"]
            p.status = "COMPLETED" if run.release == "RELEASED" else "HELD"
            self.audit.append("system", "reconciliation.completed", run.id,
                              {"release": run.release, "failed": run.reconciliation["failed"]})
        else:
            p.status = "FAILED"
        return run

    def rollback(self, actor: Principal, run_id: str) -> Run:
        if not actor.can("run:execute"):
            raise Forbidden("run:execute required")
        run = self.run(run_id)
        p = self.project(run.project_id)
        if run.status not in ("FAILED", "COMPLETED"):
            raise Conflict("run cannot be rolled back in its current state")
        self.reg(p)
        self.executor.rollback(run, self.adapters[p.target_id], actor.id)
        p.status = "ROLLED_BACK"
        return run

    def run(self, rid: str) -> Run:
        if rid not in self.runs:
            raise NotFound(f"run {rid}")
        return self.runs[rid]

    # ---------------- reports ----------------
    def report_markdown(self, run_id: str) -> str:
        run = self.run(run_id)
        p = self.project(run.project_id)
        s, t = self.system(p.source_id), self.system(p.target_id)
        rec = run.reconciliation or {"checks": [], "release": "NOT_EVALUATED", "summary": {}, "masking": {}}
        L = [f"# Refresh execution report — {p.name}", "",
             "> **SIMULATED**: executed against synthetic SAP-like data; no real SAP system was read or written.", "",
             f"- Source: {s.label}  \n- Target: {t.label}  \n- Run: `{run.id}` ({run.status}), release: **{run.release}**",
             f"- Manifest v{p.manifest.version} hash `{run.manifest_hash[:16]}…`",
             f"- Approved by: {(p.approval or {}).get('by', 'n/a')} at {(p.approval or {}).get('at', 'n/a')}", "",
             "## Scope", f"```json\n{p.manifest.scope.model_dump_json(indent=2, exclude_defaults=True)}\n```", "",
             "## Outcome", f"- Objects loaded: {len(run.loaded)}  \n- Skipped (identical in target): {len(run.skipped)}  \n"
             f"- Quarantined: {len(run.quarantined)}", ""]
        if run.quarantined:
            L += ["### Quarantined objects"] + [f"- `{q}`" for q in run.quarantined] + [""]
        L += ["## Reconciliation", "| Category | Check | Result | Detail |", "|---|---|---|---|"]
        L += [f"| {c['category']} | {c['name']} | {c['status'].upper()} | {c['detail']} |" for c in rec["checks"]]
        m = rec.get("masking", {})
        L += ["", "## Masking", f"- Protection classes: {', '.join(m.get('protection_classes', []))}",
              f"- Reversible: {m.get('reversible')}", f"- Note: {m.get('residual_risk_note', '')}", ""]
        L += ["## Approvals & audit", f"- Audit chain: {self.audit.verify()}"]
        return "\n".join(L)

    def evidence_package(self, run_id: str) -> bytes:
        run = self.run(run_id)
        p = self.project(run.project_id)
        files = {
            "manifest.json": p.manifest.model_dump(mode="json"),
            "plan_summary.json": p.plan.summary() if p.plan else {},
            "conflicts.json": p.report.to_dict() if p.report else {},
            "run.json": run.public(),
            "reconciliation.json": run.reconciliation or {},
            "staging_hashes.json": run.staging_hashes,
            "audit.json": self.audit.entries(),
            "audit_verification.json": self.audit.verify(),
        }
        buf = io.BytesIO()
        digests = {}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, obj in files.items():
                data = json.dumps(obj, indent=2, default=str).encode()
                digests[name] = sha256(data).hexdigest()
                z.writestr(name, data)
            rep = self.report_markdown(run_id).encode()
            digests["report.md"] = sha256(rep).hexdigest()
            z.writestr("report.md", rep)
            z.writestr("SHA256SUMS.json", json.dumps({"simulated": True, "files": digests}, indent=2))
        return buf.getvalue()

    # ---------------- agents ----------------
    def strategy(self, actor: Principal, pid: str, **kw) -> dict:
        p = self.project(pid)
        plan_rows = p.plan.summary()["total_rows"] if p.plan else 0
        src_rows = sum(self.adapters[p.source_id].table_counts().values())
        return advisors.refresh_strategy(source_rows=src_rows, scope_rows=plan_rows, **kw)

    def masking_advice(self, pid: str) -> dict:
        p = self.project(pid)
        pol = p.masking_policy.fields() if p.masking_policy else set()
        return advisors.masking_recommendation(self.required_sensitive.get(pid, []), pol,
                                               self.system(p.target_id).role.value)

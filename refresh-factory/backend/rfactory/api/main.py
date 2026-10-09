"""REST API. Auth is a demo header (X-Demo-User) - see security/auth.py."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..capabilities import CAPABILITIES
from ..fullrefresh import runbook
from ..masking.engine import TEMPLATES, template
from ..sap.adapter import ProductionWriteBlocked
from ..security.auth import DEMO_USERS, Forbidden, Principal
from ..selective.manifest import Scope
from ..service import Conflict, NotFound, Project, RefreshService

STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class ManifestIn(BaseModel):
    scope: Scope
    include_downstream: list[str] = Field(default_factory=list)
    masking_policy_id: str = "gdpr-standard"
    conflict_policy: dict[str, str] = Field(default_factory=dict)
    instance_overrides: dict[str, str] = Field(default_factory=dict)
    last_days: int | None = Field(None, description="Convenience: resolve date window relative to the source's reference date")


class ProjectIn(BaseModel):
    name: str
    source_id: str
    target_id: str


class PolicyIn(BaseModel):
    conflict_policy: dict[str, str] = Field(default_factory=dict)
    instance_overrides: dict[str, str] = Field(default_factory=dict)


class RulesIn(BaseModel):
    rules: list[dict]


class ExceptionIn(BaseModel):
    instance: str
    justification: str


class CommentIn(BaseModel):
    comment: str = ""


class StrategyIn(BaseModel):
    source_gb: float = 500.0
    config_change_needed: bool = False
    freshness_days: int = 30
    masking_required: bool = True
    target_free_gb: float | None = None
    cross_system_integrations: bool = False


class DeltaIn(BaseModel):
    name: str
    source_id: str
    target_id: str
    scopes: list[dict]
    include_downstream: list[str] = Field(default_factory=list)
    masking_policy_id: str = "gdpr-standard"
    conflict_policy: dict[str, str] = Field(default_factory=dict)
    schedule: dict | None = None
    full_sweep_every: int = 4
    max_objects: int = 10000

class DeltaPatch(BaseModel):
    conflict_policy: dict[str, str] | None = None
    schedule: dict | None = None
    include_downstream: list[str] | None = None
    scopes: list[dict] | None = None

class RunIn(BaseModel):
    full_sweep: bool = False

class AckIn(BaseModel):
    note: str

class TickIn(BaseModel):
    now: str | None = None



class TdmPolicyIn(BaseModel):
    name: str
    source_id: str
    target_id: str
    allowed_templates: list[str] | None = None
    max_objects: int = 10
    max_active_reservations: int = 5
    max_ttl_days: int = 14
    retention_days: int = 30
    allow_subset: bool = True
    allow_synthetic: bool = True
    masking_rules: list[dict] = Field(default_factory=list)
    auto_masking: bool = True


class TdmRequestIn(BaseModel):
    target_id: str
    template_id: str
    mode: str = "auto"
    count: int = 1
    params: dict = Field(default_factory=dict)
    purpose: str = ""
    test_cases: list[dict] = Field(default_factory=list)
    ttl_days: int | None = None
    reserve: bool = True


class ReserveIn(BaseModel):
    ttl_days: int = 7
    reason: str = ""
    test_cases: list[dict] = Field(default_factory=list)


class ReleaseIn(BaseModel):
    consumed: bool = False


class UsageIn(BaseModel):
    test_case: dict
    outcome: str
    consumed: bool = False
    note: str = ""


class PurgeIn(BaseModel):
    force: bool = False


class ReasonIn(BaseModel):
    reason: str = ""


def project_dict(svc: RefreshService, p: Project) -> dict:
    return {"id": p.id, "name": p.name, "status": p.status, "source": {**svc.system(p.source_id).model_dump(mode="json"), "family": svc.system(p.source_id).family},
            "target": {**svc.system(p.target_id).model_dump(mode="json"), "family": svc.system(p.target_id).family}, "created_by": p.created_by,
            "last_editor": p.last_editor, "submitted_by": p.submitted_by,
            "manifest": p.manifest.model_dump(mode="json") if p.manifest else None,
            "manifest_hash": p.manifest.content_hash() if p.manifest else None,
            "manifest_versions": [{"version": m.version, "hash": m.content_hash()} for m in p.manifests],
            "has_plan": p.plan is not None, "has_conflicts": p.report is not None, "approval": p.approval,
            "runs": p.runs, "created": p.created, "simulated": True}


def create_app(data_dir: Path | None = None) -> FastAPI:
    svc = RefreshService(data_dir)
    app = FastAPI(title="SAP Intelligent Refresh Factory", version="0.1.0",
                  description="MVP control plane. All SAP interaction is SIMULATED; see /api/capabilities.")
    app.state.svc = svc

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return JSONResponse({"detail": f"not found: {e.args[0]}"}, 404)

    @app.exception_handler(Forbidden)
    async def _fb(_: Request, e: Forbidden):
        return JSONResponse({"detail": str(e)}, 403)

    @app.exception_handler(ProductionWriteBlocked)
    async def _pw(_: Request, e: ProductionWriteBlocked):
        return JSONResponse({"detail": str(e)}, 403)

    @app.exception_handler(Conflict)
    async def _cf(_: Request, e: Conflict):
        return JSONResponse({"detail": str(e)}, 409)

    def me(x_demo_user: str | None = Header(None)) -> Principal:
        if not x_demo_user or x_demo_user not in DEMO_USERS:
            raise HTTPException(401, "missing or unknown X-Demo-User (demo authentication only)")
        return DEMO_USERS[x_demo_user]

    def need(perm: str):
        def dep(p: Principal = Depends(me)) -> Principal:
            if not p.can(perm):
                raise HTTPException(403, f"permission '{perm}' required")
            return p
        return dep

    # ---------------- meta ----------------
    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": "SIMULATED_SAP", "audit": svc.audit.verify()}

    @app.get("/api/me")
    def whoami(p: Principal = Depends(me)):
        return {"id": p.id, "name": p.name, "roles": p.roles, "kind": p.kind, "permissions": sorted(p.permissions())}

    @app.get("/api/users")
    def users():
        return [{"id": u.id, "name": u.name, "roles": u.roles, "kind": u.kind} for u in DEMO_USERS.values()]

    @app.get("/api/capabilities")
    def capabilities(_: Principal = Depends(need("view"))):
        keys = ("module", "feature", "status", "sap_compatibility", "constraints", "test_evidence", "remaining_work")
        return [dict(zip(keys, c)) for c in CAPABILITIES]

    # ---------------- landscape ----------------
    @app.post("/api/demo/bootstrap")
    def bootstrap(p: Principal = Depends(need("system:write"))):
        return svc.bootstrap_demo(p)

    @app.get("/api/systems")
    def systems(_: Principal = Depends(need("view"))):
        return [{**s.model_dump(mode="json"), "family": s.family, "label": s.label, "writable_target": s.can_be_write_target, "simulated": True}
                for s in svc.systems.values()]

    @app.get("/api/systems/{sid}/discovery")
    def discovery(sid: str, _: Principal = Depends(need("view"))):
        return svc.discover(sid)

    @app.get("/api/systems/{sid}/readiness")
    def readiness(sid: str, _: Principal = Depends(need("view"))):
        return svc.readiness(sid)

    @app.get("/api/landscape/combinations")
    def combos(_: Principal = Depends(need("view"))):
        return svc.refresh_combinations()

    @app.get("/api/objects/registry")
    def registry(family: str = "ECC", _: Principal = Depends(need("view"))):
        if family not in svc.registries:
            raise HTTPException(422, "family must be ECC or S4")
        return {**svc.registries[family].graph(), "family": family}

    @app.get("/api/systems/{sid}/integrity")
    def integrity(sid: str, _: Principal = Depends(need("view"))):
        from ..dependency.planner import Planner
        return Planner(svc.source_view(sid), svc.registries[svc.system(sid).family]).validate_relationships()

    # ---------------- projects ----------------
    @app.get("/api/projects")
    def projects(_: Principal = Depends(need("view"))):
        return [project_dict(svc, p) for p in svc.projects.values()]

    @app.post("/api/projects", status_code=201)
    def create_project(b: ProjectIn, p: Principal = Depends(need("project:write"))):
        return project_dict(svc, svc.create_project(p, b.name, b.source_id, b.target_id))

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, _: Principal = Depends(need("view"))):
        return project_dict(svc, svc.project(pid))

    @app.put("/api/projects/{pid}/manifest")
    def put_manifest(pid: str, b: ManifestIn, p: Principal = Depends(need("plan:write"))):
        proj = svc.project(pid)
        scope = b.scope
        if b.last_days:
            ref = svc.adapters[proj.source_id].reference_date()
            scope = scope.model_copy(update={"date_from": ref - timedelta(days=b.last_days), "date_to": ref})
        svc.set_manifest(p, pid, scope, b.include_downstream, b.masking_policy_id, b.conflict_policy, b.instance_overrides)
        return project_dict(svc, proj)

    @app.post("/api/projects/{pid}/plan")
    def plan(pid: str, p: Principal = Depends(need("plan:write"))):
        return svc.build_plan(p, pid)

    @app.get("/api/projects/{pid}/plan")
    def plan_get(pid: str, _: Principal = Depends(need("view"))):
        return svc.plan_summary(pid)

    @app.get("/api/projects/{pid}/instances")
    def instances(pid: str, type: str | None = None, origin: str | None = None, offset: int = 0,
                  limit: int = Query(100, le=500), _: Principal = Depends(need("view"))):
        return svc.instances(pid, type, origin, offset, limit)

    # ---------------- masking ----------------
    @app.get("/api/masking/templates")
    def templates(_: Principal = Depends(need("view"))):
        return [template(t).to_dict() for t in TEMPLATES]

    @app.get("/api/projects/{pid}/masking")
    def masking(pid: str, _: Principal = Depends(need("view"))):
        return svc.masking_state(pid)

    @app.post("/api/projects/{pid}/masking/rules")
    def masking_rules(pid: str, b: RulesIn, p: Principal = Depends(need("masking:write"))):
        return svc.add_masking_rules(p, pid, b.rules)

    # ---------------- conflicts ----------------
    @app.post("/api/projects/{pid}/conflicts/analyze")
    def analyze(pid: str, p: Principal = Depends(need("plan:write"))):
        return svc.analyze_conflicts(p, pid)

    @app.get("/api/projects/{pid}/conflicts")
    def conflicts(pid: str, _: Principal = Depends(need("view"))):
        return svc.conflicts(pid)

    @app.put("/api/projects/{pid}/conflicts/policy")
    def conflict_policy(pid: str, b: PolicyIn, p: Principal = Depends(need("plan:write"))):
        from ..selective.conflicts import validate_policy
        errs = validate_policy(b.conflict_policy)
        if errs:
            raise HTTPException(422, errs)
        svc.apply_conflict_policy(p, pid, b.conflict_policy, b.instance_overrides)
        return project_dict(svc, svc.project(pid))

    @app.post("/api/projects/{pid}/exceptions")
    def exception(pid: str, b: ExceptionIn, p: Principal = Depends(need("exception:approve"))):
        svc.approve_exception(p, pid, b.instance, b.justification)
        return project_dict(svc, svc.project(pid))

    # ---------------- approval / execution ----------------
    @app.post("/api/projects/{pid}/submit")
    def submit(pid: str, p: Principal = Depends(need("plan:submit"))):
        return project_dict(svc, svc.submit(p, pid))

    @app.post("/api/projects/{pid}/approve")
    def approve(pid: str, b: CommentIn, p: Principal = Depends(need("plan:approve"))):
        return project_dict(svc, svc.approve(p, pid, b.comment))

    @app.post("/api/projects/{pid}/reject")
    def reject(pid: str, b: CommentIn, p: Principal = Depends(need("plan:approve"))):
        return project_dict(svc, svc.reject(p, pid, b.comment))

    @app.post("/api/projects/{pid}/execute", status_code=202)
    def execute(pid: str, p: Principal = Depends(need("run:execute"))):
        return svc.execute(p, pid).public()

    @app.get("/api/runs/{rid}")
    def run(rid: str, _: Principal = Depends(need("view"))):
        return svc.run(rid).public()

    @app.post("/api/runs/{rid}/resume")
    def resume(rid: str, p: Principal = Depends(need("run:execute"))):
        return svc.resume(p, rid).public()

    @app.post("/api/runs/{rid}/rollback")
    def rollback(rid: str, p: Principal = Depends(need("run:execute"))):
        return svc.rollback(p, rid).public()

    @app.get("/api/runs/{rid}/reconciliation")
    def recon(rid: str, _: Principal = Depends(need("view"))):
        r = svc.run(rid)
        if not r.reconciliation:
            raise Conflict("run has no reconciliation yet")
        return r.reconciliation

    @app.get("/api/runs/{rid}/report", response_class=PlainTextResponse)
    def report(rid: str, _: Principal = Depends(need("view"))):
        return svc.report_markdown(rid)

    @app.get("/api/runs/{rid}/evidence")
    def evidence(rid: str, _: Principal = Depends(need("audit:read"))):
        return Response(svc.evidence_package(rid), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{rid}-evidence.zip"'})

    # ---------------- audit ----------------
    @app.get("/api/audit")
    def audit(resource: str | None = None, limit: int = 200, _: Principal = Depends(need("audit:read"))):
        return svc.audit.entries(resource, limit)

    @app.get("/api/audit/verify")
    def audit_verify(_: Principal = Depends(need("audit:read"))):
        return svc.audit.verify()

    # ---------------- agents (advisory only) ----------------
    @app.post("/api/projects/{pid}/agents/strategy")
    def a_strategy(pid: str, b: StrategyIn, p: Principal = Depends(need("view"))):
        r = svc.strategy(p, pid, **b.model_dump())
        svc.audit.append(p.id, "agent.strategy", pid, {"recommendation": r["recommendation"]})
        return r

    @app.get("/api/projects/{pid}/agents/masking")
    def a_masking(pid: str, _: Principal = Depends(need("view"))):
        return svc.masking_advice(pid)

    @app.get("/api/projects/{pid}/agents/conflicts")
    def a_conflicts(pid: str, _: Principal = Depends(need("view"))):
        return svc.suggest_policies(pid)

    # ---------------- full refresh (design-level) ----------------
    @app.get("/api/full-refresh/plan")
    def fr_plan(source_id: str, target_id: str, _: Principal = Depends(need("view"))):
        return runbook.plan_full_refresh(svc.system(source_id), svc.system(target_id))

    @app.get("/api/post-copy/tasks")
    def pc(_: Principal = Depends(need("view"))):
        return runbook.POST_COPY_TASKS

    # ---------------- delta refresh ----------------
    def demo_spec(source_id: str, target_id: str) -> dict:
        return {"name": "Weekend QA sync: customers, vendors, materials, sales and purchase orders (company 1000)",
                "source_id": source_id, "target_id": target_id,
                "scopes": [{"scope": {"object_type": "CUSTOMER", "company_codes": ["1000"]}},
                           {"scope": {"object_type": "VENDOR", "company_codes": ["1000"]}},
                           {"scope": {"object_type": "MATERIAL", "plants": ["1000", "1010"]}},
                           {"scope": {"object_type": "SALES_ORDER", "company_codes": ["1000"]}, "rolling_days": 90},
                           {"scope": {"object_type": "PURCHASE_ORDER", "company_codes": ["1000"]}, "rolling_days": 90}],
                "include_downstream": ["DELIVERY", "BILLING", "FI_DOCUMENT"], "masking_policy_id": "gdpr-standard",
                "conflict_policy": {"DUPLICATE_DIFFERENT": "SKIP"},
                "schedule": {"kind": "weekly", "weekday": 5, "hour": 2, "window_hours": 4}, "full_sweep_every": 4}

    @app.post("/api/delta/scenarios", status_code=201)
    def d_create(b: DeltaIn, p: Principal = Depends(need("plan:write"))):
        return svc.delta.create(p, b.model_dump()).public()

    @app.post("/api/delta/scenarios/demo", status_code=201)
    def d_demo(source_id: str, target_id: str, p: Principal = Depends(need("plan:write"))):
        return svc.delta.create(p, demo_spec(source_id, target_id)).public()

    @app.get("/api/delta/scenarios")
    def d_list(_: Principal = Depends(need("view"))):
        return [s.public() for s in svc.delta.scenarios.values()]

    @app.get("/api/delta/scenarios/{sid}")
    def d_get(sid: str, _: Principal = Depends(need("view"))):
        return svc.delta.get(sid).public()

    @app.patch("/api/delta/scenarios/{sid}")
    def d_patch(sid: str, b: DeltaPatch, p: Principal = Depends(need("plan:write"))):
        return svc.delta.update(p, sid, b.model_dump(exclude_none=True)).public()

    @app.get("/api/delta/scenarios/{sid}/masking/recommended")
    def d_mask_rec(sid: str, _: Principal = Depends(need("view"))):
        return svc.delta.recommended_rules(sid)

    @app.post("/api/delta/scenarios/{sid}/masking/rules")
    def d_mask_rules(sid: str, b: RulesIn, p: Principal = Depends(need("masking:write"))):
        return svc.delta.add_masking_rules(p, sid, b.rules).public()

    @app.get("/api/delta/scenarios/{sid}/history")
    def d_history(sid: str, _: Principal = Depends(need("view"))):
        return list(reversed(svc.delta.get(sid).history))

    @app.post("/api/delta/scenarios/{sid}/submit")
    def d_submit(sid: str, p: Principal = Depends(need("plan:submit"))):
        return svc.delta.submit(p, sid).public()

    @app.post("/api/delta/scenarios/{sid}/approve")
    def d_approve(sid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.delta.approve(p, sid).public()

    @app.post("/api/delta/scenarios/{sid}/preview")
    def d_preview(sid: str, b: RunIn, p: Principal = Depends(need("plan:write"))):
        return svc.delta.preview(p, sid, b.full_sweep)

    @app.post("/api/delta/scenarios/{sid}/run", status_code=202)
    def d_run(sid: str, b: RunIn, p: Principal = Depends(need("run:execute"))):
        return svc.delta.run(p, sid, "manual", b.full_sweep)

    @app.post("/api/delta/scenarios/{sid}/resume")
    def d_resume(sid: str, p: Principal = Depends(need("run:execute"))):
        return svc.delta.resume(p, sid)

    @app.post("/api/delta/scenarios/{sid}/rollback")
    def d_rollback(sid: str, p: Principal = Depends(need("run:execute"))):
        return svc.delta.rollback(p, sid)

    @app.post("/api/delta/scenarios/{sid}/acknowledge")
    def d_ack(sid: str, b: AckIn, p: Principal = Depends(need("run:execute"))):
        return svc.delta.acknowledge(p, sid, b.note)

    @app.post("/api/delta/tick")
    def d_tick(b: TickIn, p: Principal = Depends(need("run:execute"))):
        from datetime import datetime
        return svc.delta.tick(p, datetime.fromisoformat(b.now) if b.now else None)

    @app.post("/api/demo/simulate-source-changes")
    def sim_changes(system_id: str, p: Principal = Depends(need("system:write"))):
        from ..sap.synthetic import simulate_business_activity
        a = svc.adapters[svc.system(system_id).id]
        r = simulate_business_activity(a)
        svc.audit.append(p.id, "demo.source_activity_simulated", system_id, {"change_seq": r["change_seq"]})
        return {**r, "simulated": True}

    # ---------------- test data catalog ----------------
    @app.get("/api/tdm/templates")
    def t_templates(_: Principal = Depends(need("view"))):
        return svc.tdm.templates()

    @app.get("/api/tdm/templates/{tid}/candidates")
    def t_candidates(tid: str, source_id: str, company_code: str = "1000", days: int | None = None, limit: int = 20,
                     _: Principal = Depends(need("view"))):
        return svc.tdm.candidates(source_id, tid, {"company_code": company_code, "days": days})[:limit]

    @app.post("/api/tdm/policies", status_code=201)
    def t_policy_create(b: TdmPolicyIn, p: Principal = Depends(need("plan:write"))):
        return svc.tdm.create_policy(p, b.model_dump()).public()

    @app.get("/api/tdm/policies")
    def t_policies(_: Principal = Depends(need("view"))):
        return [x.public() for x in svc.tdm.policies.values()]

    @app.post("/api/tdm/policies/{pid}/submit")
    def t_policy_submit(pid: str, p: Principal = Depends(need("plan:submit"))):
        return svc.tdm.submit_policy(p, pid).public()

    @app.post("/api/tdm/policies/{pid}/approve")
    def t_policy_approve(pid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.tdm.approve_policy(p, pid).public()

    @app.post("/api/tdm/policies/{pid}/suspend")
    def t_policy_suspend(pid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.tdm.suspend_policy(p, pid).public()

    @app.post("/api/tdm/requests", status_code=201)
    def t_request(b: TdmRequestIn, p: Principal = Depends(need("tdm:request"))):
        return svc.tdm.request(p, b.model_dump())

    @app.get("/api/tdm/requests")
    def t_requests(_: Principal = Depends(need("view"))):
        return list(reversed(list(svc.tdm.requests.values())))

    @app.get("/api/tdm/requests/{rid}")
    def t_request_get(rid: str, _: Principal = Depends(need("view"))):
        return svc.tdm.get_request(rid)

    @app.post("/api/tdm/requests/{rid}/approve")
    def t_request_approve(rid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.tdm.approve_request(p, rid)

    @app.post("/api/tdm/requests/{rid}/reject")
    def t_request_reject(rid: str, b: ReasonIn, p: Principal = Depends(need("plan:approve"))):
        return svc.tdm.reject_request(p, rid, b.reason)

    @app.get("/api/tdm/catalog")
    def t_catalog(target_id: str | None = None, template_id: str | None = None, state: str | None = None,
                  company_code: str | None = None, provenance: str | None = None, test_case: str | None = None,
                  reserved_by: str | None = None, q: str | None = None, include_inactive: bool = False,
                  _: Principal = Depends(need("view"))):
        return svc.tdm.catalog(target_id=target_id, template_id=template_id, state=state, company_code=company_code,
                               provenance=provenance, test_case=test_case, reserved_by=reserved_by, q=q,
                               include_inactive=include_inactive)

    @app.get("/api/tdm/datasets/{did}")
    def t_dataset(did: str, _: Principal = Depends(need("view"))):
        return svc.tdm.get(did).public()

    @app.get("/api/tdm/datasets/{did}/handles")
    def t_handles(did: str, _: Principal = Depends(need("view"))):
        d = svc.tdm.get(did)
        return {"dataset": d.id, "state": d.state, "target_id": d.target_id, "handles": d.handles, "attrs": d.attrs}

    @app.get("/api/tdm/datasets/{did}/verify")
    def t_verify(did: str, _: Principal = Depends(need("view"))):
        return svc.tdm.verify(did)

    @app.post("/api/tdm/datasets/{did}/reserve")
    def t_reserve(did: str, b: ReserveIn, p: Principal = Depends(need("tdm:request"))):
        return svc.tdm.reserve(p, did, b.ttl_days, b.reason, b.test_cases).public()

    @app.post("/api/tdm/datasets/{did}/release")
    def t_release(did: str, b: ReleaseIn, p: Principal = Depends(need("tdm:request"))):
        return svc.tdm.release(p, did, b.consumed).public()

    @app.post("/api/tdm/datasets/{did}/usage")
    def t_usage(did: str, b: UsageIn, p: Principal = Depends(need("tdm:request"))):
        return svc.tdm.record_usage(p, did, b.test_case, b.outcome, b.consumed, b.note).public()

    @app.post("/api/tdm/datasets/{did}/test-cases")
    def t_link(did: str, tc: dict, p: Principal = Depends(need("tdm:request"))):
        return svc.tdm.link_test_case(p, did, tc).public()

    @app.post("/api/tdm/datasets/{did}/golden")
    def t_golden(did: str, p: Principal = Depends(need("tdm:curate"))):
        return svc.tdm.promote_golden(p, did).public()

    @app.post("/api/tdm/datasets/{did}/restore")
    def t_restore(did: str, p: Principal = Depends(need("tdm:curate"))):
        return svc.tdm.restore(p, did).public()

    @app.post("/api/tdm/datasets/{did}/retire")
    def t_retire(did: str, p: Principal = Depends(need("tdm:curate"))):
        return svc.tdm.retire(p, did).public()

    @app.post("/api/tdm/datasets/{did}/purge")
    def t_purge(did: str, b: PurgeIn, p: Principal = Depends(need("plan:approve"))):
        return svc.tdm.purge(p, did, b.force)

    @app.post("/api/tdm/scan")
    def t_scan(target_id: str, company_code: str = "1000", p: Principal = Depends(need("tdm:curate"))):
        return svc.tdm.scan(p, target_id, company_code)

    @app.post("/api/tdm/sweep")
    def t_sweep(p: Principal = Depends(need("run:execute"))):
        return svc.tdm.sweep(p)

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app


app = create_app()

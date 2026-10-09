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


def project_dict(svc: RefreshService, p: Project) -> dict:
    return {"id": p.id, "name": p.name, "status": p.status, "source": svc.system(p.source_id).model_dump(mode="json"),
            "target": svc.system(p.target_id).model_dump(mode="json"), "created_by": p.created_by,
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
        return [{**s.model_dump(mode="json"), "label": s.label, "writable_target": s.can_be_write_target, "simulated": True}
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
    def registry(_: Principal = Depends(need("view"))):
        return svc.registry.graph()

    @app.get("/api/systems/{sid}/integrity")
    def integrity(sid: str, _: Principal = Depends(need("view"))):
        from ..dependency.planner import Planner
        return Planner(svc.source_view(sid), svc.registry).validate_relationships()

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

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app


app = create_app()

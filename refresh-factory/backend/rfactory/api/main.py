"""REST API. Auth is a demo header (X-Demo-User) - see security/auth.py."""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..capabilities import CAPABILITIES
from ..fullrefresh import runbook
from ..masking.engine import TEMPLATES, template
from ..sap.adapter import ProductionWriteBlocked
from ..security import authz
from ..security.auth import DEMO_USERS, Forbidden, Principal
from ..security.oidc import AuthConfig, AuthError, OidcVerifier
from . import hardening
from ..selective.manifest import Scope
from ..service import Conflict, NotFound, Project, RefreshService

STATIC = Path(__file__).resolve().parents[3] / "frontend" / "dist"


class AnalysisIn(BaseModel):
    table: str
    fields: list[str] = []
    top: int = 20
    date_field: str | None = None
    period: str = "month"


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


class FullRefreshIn(BaseModel):
    name: str | None = None
    source_id: str
    target_id: str
    profile_id: str
    backup_ref: str
    masking_policy_id: str = "gdpr-standard"
    mechanism: str = "simulated-homogeneous-copy"


class BenchRunIn(BaseModel):
    source_id: str
    windows: list[int] = [15, 30, 60, 90, 120, 180]
    repeats: int = 2
    company_code: str = "1000"


class BenchSampleIn(BaseModel):
    phase: str
    rows: int
    seconds: float
    bytes: int = 0
    environment: str
    label: str = ""


class BenchBindIn(BaseModel):
    system_id: str
    environment: str


class BenchEstimateIn(BaseModel):
    rows: int
    bytes: int = 0
    source_id: str
    target_id: str | None = None


class ApprovalIn(BaseModel):
    label: str


class JobIn(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)
    priority: str = "normal"
    after: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    max_attempts: int = 3


class PipelineIn(BaseModel):
    name: str = "pipeline"
    steps: list[dict]


class ScheduleIn(BaseModel):
    name: str = "schedule"
    schedule: dict
    template: dict


class WindowsIn(BaseModel):
    allow: list[dict] = Field(default_factory=list)
    blackouts: list[dict] = Field(default_factory=list)


class SubscriptionIn(BaseModel):
    channel: str
    destination: str
    events: list[str] = Field(default_factory=list)
    min_severity: str = "warn"


class ClockIn(BaseModel):
    hours: float


class GateIn(BaseModel):
    note: str = ""


class ConnectIn(BaseModel):
    system: dict
    profile: dict


class SmokeIn(BaseModel):
    system: dict
    profile: dict
    tables: list[str] = Field(default_factory=list)
    max_rows: int = 500
    confirm: bool = False  # "this is a sandbox or a copy and the user is read-only"


class WriteRequestIn(BaseModel):
    note: str = ""
    attest_outbound_inactive: bool = False  # "I checked that the system's outbound interfaces / jobs are inactive"


class RevokeIn(BaseModel):
    subject: str | None = None
    jti: str | None = None
    exp: float | None = None  # when the token would have expired (so the entry can be dropped afterwards)
    reason: str = ""


class AgentRunIn(BaseModel):
    params: dict = Field(default_factory=dict)
    narrate: bool = False


class DecisionIn(BaseModel):
    note: str | None = None


class CopilotIn(BaseModel):
    question: str
    params: dict = Field(default_factory=dict)


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


class EnvTemplateIn(BaseModel):
    name: str
    source_id: str
    purpose: str = "sandbox"
    company_codes: list[str] = Field(default_factory=list)
    masters: list[dict] = Field(default_factory=list)
    transactions: list[dict] = Field(default_factory=list)
    masking_policy_id: str = "gdpr-standard"
    masking_rules: list[dict] = Field(default_factory=list)
    max_rows: int = 1000
    retention_days: int = 30
    protect_after_build: bool | None = None
    auto_masking: bool = True


class EstimateIn(BaseModel):
    host_id: str


class BuildIn(BaseModel):
    template_id: str
    host_id: str
    client: str
    name: str | None = None
    logical_system: str | None = None


class ProtectIn(BaseModel):
    locked: bool


class CaptureIn(BaseModel):
    system_id: str
    name: str


class PcRunIn(BaseModel):
    target_id: str
    profile_id: str
    source_id: str | None = None
    tasks: list[str] | None = None
    mode: str = "deactivate"


class LabelIn(BaseModel):
    label: str


def project_dict(svc: RefreshService, p: Project) -> dict:
    return {"id": p.id, "name": p.name, "status": p.status, "source": {**svc.system(p.source_id).model_dump(mode="json"), "family": svc.system(p.source_id).family},
            "target": {**svc.system(p.target_id).model_dump(mode="json"), "family": svc.system(p.target_id).family}, "created_by": p.created_by,
            "last_editor": p.last_editor, "submitted_by": p.submitted_by,
            "manifest": p.manifest.model_dump(mode="json") if p.manifest else None,
            "manifest_hash": p.manifest.content_hash() if p.manifest else None,
            "manifest_versions": [{"version": m.version, "hash": m.content_hash()} for m in p.manifests],
            "has_plan": p.plan is not None, "has_conflicts": p.report is not None, "approval": p.approval,
            "runs": p.runs, "created": p.created, "simulated": True}


def create_app(data_dir: Path | None = None, persist: bool | None = None, auth: AuthConfig | None = None, jwks: dict | None = None) -> FastAPI:
    """`RFACTORY_DATA_DIR` makes the platform durable (state survives restarts); without it, or with persist=False, state is in memory."""
    env_dir = os.environ.get("RFACTORY_DATA_DIR")
    if data_dir is None and env_dir:
        data_dir = Path(env_dir)
    if persist is None:
        persist = bool(env_dir)
    svc = RefreshService(data_dir, persist=persist)
    auth = auth or AuthConfig.from_env()
    auth.check_login()
    production = os.environ.get("RFACTORY_ENV", "").lower() == "production"
    if production:
        problems = hardening.production_problems(auth, persist)
        if problems:
            raise RuntimeError("RFACTORY_ENV=production refuses to start: " + "; ".join(problems))
    verifier = OidcVerifier(auth, jwks) if auth.mode == "oidc" else None
    failures: list[float] = []
    app = FastAPI(title="SAP Intelligent Refresh Factory", version="0.1.0",
                  description="MVP control plane. All SAP interaction is SIMULATED; see /api/capabilities.")
    app.state.svc = svc
    app.state.verifier = verifier
    if verifier is not None:
        verifier.revocations = svc.revocations

    write_lock = asyncio.Lock()
    sec_headers = hardening.headers(auth, os.environ.get("RFACTORY_HSTS") == "1")

    @app.middleware("http")
    async def hardening_mw(request: Request, call_next):
        """Body limit, demo endpoints off in production, and the security headers on every answer."""
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > hardening.MAX_BODY_BYTES:
            resp = JSONResponse({"detail": f"request body larger than {hardening.MAX_BODY_BYTES} bytes"}, 413)
        elif production and request.url.path.startswith("/api/demo/"):
            resp = JSONResponse({"detail": "demo endpoints are disabled in production"}, 404)
        else:
            resp = await call_next(request)
        for k, v in sec_headers.items():
            resp.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.middleware("http")
    async def single_writer(request: Request, call_next):
        """Mutating requests run one at a time and the state is saved at the request boundary, so what is on disk is always consistent."""
        if request.method in ("GET", "HEAD", "OPTIONS") or svc.store is None:
            return await call_next(request)
        async with write_lock:
            resp = await call_next(request)
            try:
                await run_in_threadpool(svc.checkpoint)
            except Exception as e:  # noqa: BLE001 - the effect happened in memory but is NOT durable: say so loudly
                return JSONResponse({"detail": f"the request was applied but the state could not be saved: {type(e).__name__}: {e}"}, 500)
            return resp

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

    def _auth_failed(request: Request, reason: str, detail: str) -> HTTPException:
        import time
        now = time.time()
        failures[:] = [t for t in failures if now - t < 60]
        if len(failures) < 20:  # cap audit growth under a credential-guessing flood
            failures.append(now)
            svc.audit.append("anonymous", "auth.failed", request.url.path, {"reason": reason, "client": request.client.host if request.client else None})
        return HTTPException(401, detail, headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'} if verifier else None)

    def me(request: Request, x_demo_user: str | None = Header(None), authorization: str | None = Header(None)) -> Principal:
        if verifier is not None:  # production-style mode: the demo header is ignored entirely
            if not authorization or not authorization.lower().startswith("bearer "):
                raise _auth_failed(request, "missing", "a bearer token is required")
            try:
                claims = verifier.verify_claims(authorization[7:].strip())
                p = verifier.principal(claims)
                request.state.claims = claims
            except AuthError as e:
                raise _auth_failed(request, e.reason, f"invalid token ({e.reason})")
        else:
            if not x_demo_user or x_demo_user not in DEMO_USERS:
                raise HTTPException(401, "missing or unknown X-Demo-User (demo authentication only)")
            p = DEMO_USERS[x_demo_user]
        authz.check_path(svc, p, request.url.path, request.query_params)
        return p

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
        return {"id": p.id, "name": p.name, "roles": p.roles, "kind": p.kind, "permissions": sorted(p.permissions()), "scope": p.attrs or None,
                "auth": auth.mode}

    def _need_oidc():
        if verifier is None:
            raise HTTPException(409, "token revocation applies to oidc mode only (demo authentication has no tokens)")

    @app.post("/api/auth/logout")
    def logout(request: Request, p: Principal = Depends(me)):
        """Revokes the token this request was made with (so a stolen copy stops working too)."""
        _need_oidc()
        c = request.state.claims
        if not c.get("jti"):
            return {"revoked": False, "reason": "this token has no jti, so it cannot be revoked individually: use logout-all"}
        svc.revocations.revoke_token(str(c["jti"]), c.get("exp", 0), p.id, "logout")
        svc.audit.append(p.id, "auth.logout", p.id, {"jti": str(c["jti"])[:8]})
        return {"revoked": True}

    @app.post("/api/auth/logout-all")
    def logout_all(p: Principal = Depends(me)):
        """Revokes every token this person holds that was issued up to now."""
        _need_oidc()
        cut = svc.revocations.revoke_subject(p.id, p.id, "logout-all")
        svc.audit.append(p.id, "auth.logout_all", p.id, {"cutoff": cut})
        return {"revoked": True, "cutoff": cut}

    @app.post("/api/auth/revoke")
    def revoke(b: RevokeIn, p: Principal = Depends(need("token:revoke"))):
        _need_oidc()
        if p.kind != "human":
            raise HTTPException(403, "a human is required")
        if bool(b.subject) == bool(b.jti):
            raise HTTPException(422, "give either a subject or a jti")
        if b.subject:
            cut = svc.revocations.revoke_subject(b.subject, p.id, b.reason)
            svc.audit.append(p.id, "auth.revoked", b.subject, {"kind": "subject", "reason": b.reason[:100], "cutoff": cut})
            return {"revoked": True, "subject": b.subject, "cutoff": cut}
        svc.revocations.revoke_token(b.jti, b.exp or (__import__("time").time() + 86_400), p.id, b.reason)
        svc.audit.append(p.id, "auth.revoked", b.jti[:8], {"kind": "token", "reason": b.reason[:100]})
        return {"revoked": True, "jti": b.jti[:8] + "…"}

    @app.get("/api/auth/revocations")
    def revocations(_: Principal = Depends(need("token:revoke"))):
        return svc.revocations.public()

    @app.get("/api/auth/config")
    def auth_config():
        return auth.public()

    @app.get("/api/users")
    def users():
        if verifier is not None:
            return []  # no demo directory in oidc mode
        return [{"id": u.id, "name": u.name, "roles": u.roles, "kind": u.kind, "scope": u.attrs or None} for u in DEMO_USERS.values()]

    @app.get("/api/capabilities")
    def capabilities(_: Principal = Depends(need("view"))):
        keys = ("module", "feature", "status", "sap_compatibility", "constraints", "test_evidence", "remaining_work")
        return [dict(zip(keys, c)) for c in CAPABILITIES]

    # ---------------- landscape ----------------
    @app.post("/api/demo/bootstrap")
    def bootstrap(p: Principal = Depends(need("system:write"))):
        return svc.bootstrap_demo(p)

    @app.get("/api/systems")
    def systems(p: Principal = Depends(need("view"))):
        return [{**s.model_dump(mode="json"), "family": s.family, "label": s.label, "writable_target": s.can_be_write_target, "simulated": True}
                for s in svc.systems.values() if authz.system_ok(p, s)]

    @app.post("/api/systems/connect", status_code=201)
    def connect_remote(b: ConnectIn, p: Principal = Depends(me)):
        from ..sap.adapter import SapSystem
        from ..sap.connectors.profile import ConnectionProfile
        try:
            system, profile = SapSystem(**{"id": "", **b.system}), ConnectionProfile(**b.profile)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid system or profile: {e}")
        s = svc.connect_remote(p, system, profile)
        return {**s.model_dump(mode="json"), "label": s.label, "remote": True, "writable_target": False}

    @app.post("/api/systems/smoke")
    def smoke_remote(b: SmokeIn, p: Principal = Depends(me)):
        """Read-only, bounded first-contact test of a connection profile (nothing is registered). The report has no row values."""
        from ..sap.adapter import SapSystem
        from ..sap.connectors.profile import ConnectionProfile
        if not b.confirm:
            raise HTTPException(422, "confirm that this is a sandbox or a copy and that the SAP user is read-only")
        try:
            system, profile = SapSystem(**{"id": "", **b.system}), ConnectionProfile(**b.profile)
        except (TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid system or profile: {e}")
        return svc.smoke_remote(p, system, profile, b.tables, b.max_rows)

    @app.post("/api/systems/{sid}/write-request")
    def write_request(sid: str, b: WriteRequestIn, p: Principal = Depends(me)):
        return svc.request_remote_write(p, sid, b.note, b.attest_outbound_inactive)

    @app.post("/api/systems/{sid}/write-approve")
    def write_approve(sid: str, p: Principal = Depends(me)):
        return svc.approve_remote_write(p, sid)

    @app.post("/api/systems/{sid}/write-revoke")
    def write_revoke(sid: str, p: Principal = Depends(me)):
        return svc.revoke_remote_write(p, sid)

    @app.get("/api/systems/{sid}/remote")
    def remote_info(sid: str, _: Principal = Depends(need("view"))):
        svc.system(sid)
        if svc.is_local(sid):
            raise HTTPException(409, "this system is simulated, not remote")
        a, prof = svc.adapters[sid], svc.remote_profiles.get(sid)
        return {"profile": prof.public() if prof else None, "capabilities": a.capabilities(), "stats": a.stats.public(), "schema_drift": a.drift,
                "change_documents": svc.change_doc_info(sid), "write": {"request": svc.write_requests.get(sid), "writable": svc.system(sid).can_be_write_target,
                                                                  "approval": (prof.options.get("write") if prof else None)}, "gaps": a.gaps() if hasattr(a, "gaps") else {}, "validated_against_real_sap": False}

    @app.post("/api/demo/connect-fake-rfc", status_code=201)
    def demo_connect_fake(change_documents: bool = False, p: Principal = Depends(need("system:write"))):
        """Registers a second ECC production source that is reached through the RFC adapter over a FAKE RFC transport (no SAP involved)."""
        from ..sap.adapter import SapSystem
        from ..sap.connectors.fake_rfc import FakeRfcTransport
        from ..sap.connectors.profile import ConnectionProfile
        from ..sap.synthetic import make_demo_pair
        sim, _t = make_demo_pair()
        system = SapSystem(sid="EP2", client="100", role="PRD", owner="finance-ops", tags=["remote-demo"])
        prof = ConnectionProfile("EP2 via fake RFC", "rfc", ashost="fake.invalid", client="100", user="DEMO", password_ref="env:DEMO_NOT_USED", calls_per_minute=60_000,
                                 options={"change_documents": True} if change_documents else {})
        s = svc.connect_remote(p, system, prof, transport=FakeRfcTransport(sim), reference=sim.reference_date)
        return {**s.model_dump(mode="json"), "label": s.label, "remote": True, "simulated_transport": True}

    @app.post("/api/demo/connect-fake-odata", status_code=201)
    def demo_connect_fake_odata(p: Principal = Depends(need("system:write"))):
        """Registers an S/4HANA-style source reached through the OData adapter over a FAKE OData endpoint (no SAP involved)."""
        from ..sap.adapter import SapSystem
        from ..sap.connectors.fake_odata import FakeODataTransport
        from ..sap.connectors.profile import ConnectionProfile
        from ..sap.synthetic import make_demo_pair
        sim, _t = make_demo_pair()
        system = SapSystem(sid="S4X", client="100", role="PRD", owner="finance-ops", tags=["remote-demo", "odata"])
        prof = ConnectionProfile("S4X via fake OData", "odata", base_url="https://fake-s4.invalid", user="DEMO", password_ref="env:DEMO_NOT_USED", calls_per_minute=60_000)
        s = svc.connect_remote(p, system, prof, transport=FakeODataTransport(sim), reference=sim.reference_date)
        return {**s.model_dump(mode="json"), "label": s.label, "remote": True, "simulated_transport": True}

    @app.get("/api/systems/{sid}/discovery")
    def discovery(sid: str, a: Principal = Depends(need("view"))):
        return authz.filter_discovery(a, svc.discover(sid))

    @app.get("/api/systems/{sid}/analysis")
    def analysis_catalog(sid: str, a: Principal = Depends(need("view"))):
        authz.require_systems(svc, a, sid)
        return svc.analysis_catalog(sid)

    @app.post("/api/systems/{sid}/analysis/{kind}")
    def analysis_run(sid: str, kind: str, b: AnalysisIn, a: Principal = Depends(need("view"))):
        """Read-only data analysis of one table: distribution (TAANA style), selectivity (DB05 style) or growth by creation period."""
        authz.require_systems(svc, a, sid)
        if kind == "growth" and not b.date_field:
            raise HTTPException(422, "growth needs date_field")
        return svc.analyze(a, sid, kind, b.table, fields=b.fields, top=b.top, date_field=b.date_field, period=b.period)

    @app.get("/api/systems/{sid}/readiness")
    def readiness(sid: str, _: Principal = Depends(need("view"))):
        return svc.readiness(sid)

    @app.get("/api/landscape/combinations")
    def combos(a: Principal = Depends(need("view"))):
        return [c for c in svc.refresh_combinations() if authz.visible(svc, a, c["source"], c["target"])]

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
    def projects(a: Principal = Depends(need("view"))):
        return [project_dict(svc, p) for p in svc.projects.values() if authz.visible(svc, a, p.source_id, p.target_id)]

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
    def audit_verify(expected_seq: int | None = None, expected_head: str | None = None, expected_ts: str | None = None,
                     expected_signature: str | None = None, _: Principal = Depends(need("audit:read"))):
        """Verify the chain and signatures; pass an escrowed head (from /api/audit/head) to also detect truncation."""
        if expected_seq is not None and expected_head:
            return svc.audit.verify({"seq": expected_seq, "head": expected_head, "ts": expected_ts, "signature": expected_signature})
        return svc.audit.verify()

    @app.get("/api/audit/head")
    def audit_head(_: Principal = Depends(need("audit:read"))):
        """A signed (seq, hash) of the log's end. Keep a copy outside the platform: it is what makes truncation detectable."""
        return svc.audit.head()

    @app.get("/api/persistence/status")
    def persistence_status(_: Principal = Depends(need("view"))):
        if svc.store is None:
            return {"durable": False, "note": "state is in memory and is lost on restart; set RFACTORY_DATA_DIR to make it durable"}
        return svc.store.status()

    @app.post("/api/persistence/rotate-data-key")
    def persistence_rotate(p: Principal = Depends(me)):
        """Re-encrypt every stored object under a fresh data key (online). Rotating the key-encryption key is an offline operator task."""
        if p.kind != "human" or not p.can("system:write"):
            raise HTTPException(403, "a human with system:write is required")
        if svc.store is None:
            raise HTTPException(409, "the platform is not durable (no RFACTORY_DATA_DIR)")
        n = svc.store.rotate_dek()
        svc.audit.append(p.id, "persistence.data_key_rotated", "store", {"blobs": n})
        return {"blobs_reencrypted": n, **svc.store.status()}

    @app.post("/api/persistence/checkpoint")
    def persistence_checkpoint(_: Principal = Depends(need("system:write"))):
        if svc.store is None:
            raise HTTPException(409, "the platform is not durable (no RFACTORY_DATA_DIR)")
        return {"saved": svc.checkpoint(), **svc.store.status()}

    # ---------------- AI refresh agents (module 12) ----------------
    from ..agents.service import AgentError

    def _agent(fn, *a, **k):
        try:
            return fn(*a, **k)
        except AgentError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except KeyError as e:
            raise HTTPException(404, f"not found: {e.args[0]}")

    @app.get("/api/agents")
    def ag_catalogue(_: Principal = Depends(need("view"))):
        return svc.agents.catalogue()

    @app.post("/api/agents/copilot")
    def ag_copilot(b: CopilotIn, p: Principal = Depends(need("view"))):
        return _agent(svc.agents.copilot, p, b.question, b.params)

    @app.get("/api/agents/reports")
    def ag_reports(agent_id: str | None = None, _: Principal = Depends(need("view"))):
        return svc.agents.list_reports(agent_id)

    @app.get("/api/agents/reports/{rid}")
    def ag_report(rid: str, _: Principal = Depends(need("view"))):
        return _agent(svc.agents.report, rid)

    @app.get("/api/agents/recommendations")
    def ag_recs(status: str | None = None, agent_id: str | None = None, _: Principal = Depends(need("view"))):
        return svc.agents.list_recs(status, agent_id)

    @app.post("/api/agents/recommendations/{rid}/accept")
    def ag_accept(rid: str, b: DecisionIn, p: Principal = Depends(me)):
        return _agent(svc.agents.accept, p, rid, b.note)

    @app.post("/api/agents/recommendations/{rid}/reject")
    def ag_reject(rid: str, b: DecisionIn, p: Principal = Depends(me)):
        return _agent(svc.agents.reject, p, rid, b.note)

    @app.post("/api/agents/recommendations/{rid}/apply")
    def ag_apply(rid: str, p: Principal = Depends(me)):
        return _agent(svc.agents.apply, p, rid)

    @app.post("/api/agents/{agent_id}/run")
    def ag_run(agent_id: str, b: AgentRunIn, p: Principal = Depends(need("view"))):
        return _agent(svc.agents.run, p, agent_id, b.params, b.narrate)

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

    @app.post("/api/full-refresh/programs", status_code=201)
    def fr_create(b: FullRefreshIn, p: Principal = Depends(me)):
        return svc.full.create(p, b.model_dump()).public()

    @app.get("/api/full-refresh/programs")
    def fr_list(a: Principal = Depends(need("view"))):
        return [x.public() for x in reversed(list(svc.full.programs.values())) if authz.visible(svc, a, x.source_id, x.target_id)]

    @app.get("/api/full-refresh/programs/{pid}")
    def fr_get(pid: str, _: Principal = Depends(need("view"))):
        return svc.full.get(pid).public()

    @app.post("/api/full-refresh/programs/{pid}/approve")
    def fr_approve(pid: str, b: ApprovalIn, p: Principal = Depends(me)):
        return svc.full.approve(p, pid, b.label).public()

    @app.post("/api/full-refresh/programs/{pid}/run")
    def fr_run(pid: str, p: Principal = Depends(me)):
        return svc.full.run(p, pid).public()

    @app.post("/api/full-refresh/programs/{pid}/security-signoff")
    def fr_signoff(pid: str, p: Principal = Depends(me)):
        return svc.full.sign_off(p, pid).public()

    @app.post("/api/full-refresh/programs/{pid}/release")
    def fr_release(pid: str, p: Principal = Depends(me)):
        return svc.full.release(p, pid).public()

    @app.post("/api/full-refresh/programs/{pid}/rollback")
    def fr_rollback(pid: str, p: Principal = Depends(me)):
        return svc.full.rollback(p, pid).public()

    @app.get("/api/full-refresh/programs/{pid}/evidence")
    def fr_evidence(pid: str, _: Principal = Depends(need("audit:read"))):
        return svc.full.evidence_report(pid)

    # ---------------- benchmarks and calibrated estimates ----------------
    @app.get("/api/benchmark/summary")
    def bench_summary(_: Principal = Depends(need("view"))):
        return svc.bench.summary()

    @app.get("/api/benchmark/samples")
    def bench_samples(phase: str | None = None, environment: str | None = None, _: Principal = Depends(need("view"))):
        return [s.public() for s in reversed(svc.bench.samples) if (not phase or s.phase == phase) and (not environment or s.environment == environment)][:500]

    @app.post("/api/benchmark/run")
    def bench_run(b: BenchRunIn, p: Principal = Depends(me)):
        if len(b.windows) > 12 or not 1 <= b.repeats <= 5 or any(w < 1 or w > 3650 for w in b.windows):
            raise HTTPException(422, "at most 12 windows of 1-3650 days, 1-5 repeats")
        return svc.bench.run_benchmark(p, b.source_id, tuple(b.windows), b.repeats, b.company_code)

    @app.post("/api/benchmark/samples", status_code=201)
    def bench_import(b: BenchSampleIn, p: Principal = Depends(me)):
        return svc.bench.import_sample(p, b.model_dump()).public()

    @app.post("/api/benchmark/samples/{sid}/exclude")
    def bench_exclude(sid: str, excluded: bool = True, p: Principal = Depends(me)):
        return svc.bench.exclude(p, sid, excluded).public()

    @app.post("/api/benchmark/bind")
    def bench_bind(b: BenchBindIn, p: Principal = Depends(me)):
        return svc.bench.bind(p, b.system_id, b.environment)

    @app.post("/api/benchmark/estimate")
    def bench_estimate(b: BenchEstimateIn, a: Principal = Depends(need("view"))):
        for sid in (b.source_id, b.target_id):
            if sid:
                svc.system(sid)
                if not authz.visible(svc, a, sid):
                    raise HTTPException(403, f"no access to {sid}")
        return svc.bench.estimate(b.rows, b.bytes, b.source_id, b.target_id)

    # ---------------- orchestration (module 14) ----------------
    def _orch(fn, *a, **k):
        try:
            return fn(*a, **k)
        except KeyError as e:
            raise HTTPException(422, f"missing or invalid field: {e}")

    @app.get("/api/orchestration/summary")
    def or_summary(_: Principal = Depends(need("view"))):
        return {**svc.orch.summary(), "kinds": {k: d for k, (_p, d) in __import__("rfactory.orchestration.engine", fromlist=["KINDS"]).KINDS.items()}}

    @app.post("/api/orchestration/jobs", status_code=201)
    def or_submit(b: JobIn, p: Principal = Depends(me)):
        return _orch(svc.orch.submit, p, b.kind, b.params, b.priority, b.after, None, b.idempotency_key, b.max_attempts).public()

    @app.get("/api/orchestration/jobs")
    def or_jobs(status: str | None = None, a: Principal = Depends(need("view"))):
        return [j.public() for j in reversed(list(svc.orch.jobs.values())) if (not status or j.status == status) and authz.visible(svc, a, j.target_id)]

    @app.get("/api/orchestration/jobs/{jid}")
    def or_job(jid: str, _: Principal = Depends(need("view"))):
        return svc.orch.get(jid).public()

    @app.post("/api/orchestration/jobs/{jid}/cancel")
    def or_cancel(jid: str, p: Principal = Depends(me)):
        return svc.orch.cancel(p, jid).public()

    @app.post("/api/orchestration/jobs/{jid}/complete")
    def or_gate(jid: str, b: GateIn, p: Principal = Depends(me)):
        return svc.orch.complete_gate(p, jid, b.note).public()

    @app.post("/api/orchestration/pipelines", status_code=201)
    def or_pipeline(b: PipelineIn, p: Principal = Depends(me)):
        return _orch(svc.orch.submit_pipeline, p, b.model_dump())

    @app.get("/api/orchestration/pipelines")
    def or_pipelines(a: Principal = Depends(need("view"))):
        return [pl for pl in (svc.orch.pipeline(k) for k in reversed(list(svc.orch.pipelines)))
                if all(authz.visible(svc, a, j["target_id"]) for j in pl["jobs_detail"])]

    @app.post("/api/orchestration/schedules", status_code=201)
    def or_sched(b: ScheduleIn, p: Principal = Depends(me)):
        return svc.orch.create_schedule(p, b.model_dump()).public()

    @app.get("/api/orchestration/schedules")
    def or_scheds(_: Principal = Depends(need("view"))):
        return [s.public() for s in svc.orch.schedules.values()]

    @app.post("/api/orchestration/schedules/{sid}/approve")
    def or_sched_ok(sid: str, p: Principal = Depends(me)):
        return svc.orch.approve_schedule(p, sid).public()

    @app.post("/api/orchestration/schedules/{sid}/pause")
    def or_sched_pause(sid: str, p: Principal = Depends(me)):
        return svc.orch.pause_schedule(p, sid, True).public()

    @app.post("/api/orchestration/schedules/{sid}/resume")
    def or_sched_resume(sid: str, p: Principal = Depends(me)):
        return svc.orch.pause_schedule(p, sid, False).public()

    @app.get("/api/orchestration/windows/{sysid}")
    def or_win(sysid: str, _: Principal = Depends(need("view"))):
        return svc.orch.window_state(sysid)

    @app.put("/api/orchestration/windows/{sysid}")
    def or_win_set(sysid: str, b: WindowsIn, p: Principal = Depends(me)):
        return _orch(svc.orch.set_windows, p, sysid, b.model_dump())

    @app.post("/api/orchestration/tick")
    def or_tick(p: Principal = Depends(me)):
        return svc.orch.tick(p)

    @app.post("/api/orchestration/clock")
    def or_clock(b: ClockIn, p: Principal = Depends(me)):
        return svc.orch.advance_clock(p, b.hours)

    @app.get("/api/orchestration/events")
    def or_events(limit: int = 100, _: Principal = Depends(need("view"))):
        return list(reversed(svc.orch.events[-limit:]))

    @app.get("/api/orchestration/outbox")
    def or_outbox(_: Principal = Depends(need("view"))):
        return list(reversed(svc.orch.outbox[-100:]))

    @app.get("/api/orchestration/subscriptions")
    def or_subs(_: Principal = Depends(need("view"))):
        return list(svc.orch.subs.values())

    @app.post("/api/orchestration/subscriptions", status_code=201)
    def or_sub(b: SubscriptionIn, p: Principal = Depends(me)):
        return svc.orch.subscribe(p, b.model_dump())

    @app.delete("/api/orchestration/subscriptions/{sid}")
    def or_unsub(sid: str, p: Principal = Depends(me)):
        return svc.orch.unsubscribe(p, sid)

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
    def d_list(a: Principal = Depends(need("view"))):
        return [s.public() for s in svc.delta.scenarios.values() if authz.visible(svc, a, s.source_id, s.target_id)]

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
                     a: Principal = Depends(need("view"))):
        authz.require_systems(svc, a, source_id)
        if authz.restricted(a):
            authz.require_companies(a, [company_code], "company_code")
        return svc.tdm.candidates(source_id, tid, {"company_code": company_code, "days": days})[:limit]

    @app.post("/api/tdm/policies", status_code=201)
    def t_policy_create(b: TdmPolicyIn, p: Principal = Depends(need("plan:write"))):
        return svc.tdm.create_policy(p, b.model_dump()).public()

    @app.get("/api/tdm/policies")
    def t_policies(a: Principal = Depends(need("view"))):
        return [x.public() for x in svc.tdm.policies.values() if authz.visible(svc, a, x.target_id)]

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
    def t_requests(a: Principal = Depends(need("view"))):
        return [r for r in reversed(list(svc.tdm.requests.values())) if authz.visible(svc, a, r["spec"]["target_id"])]

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
                  a: Principal = Depends(need("view"))):
        return [d for d in svc.tdm.catalog(target_id=target_id, template_id=template_id, state=state, company_code=company_code,
                                           provenance=provenance, test_case=test_case, reserved_by=reserved_by, q=q,
                                           include_inactive=include_inactive) if authz.visible(svc, a, d.get("target_id"))]

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

    # ---------------- lean client builder ----------------
    @app.get("/api/lean/profiles")
    def l_profiles(_: Principal = Depends(need("view"))):
        return svc.lean.profiles()

    @app.get("/api/lean/presets")
    def l_presets(source_id: str, company_code: str = "1000", _: Principal = Depends(need("view"))):
        from ..leanclient.profiles import PURPOSES, preset
        return [{**preset(p, company_code), "source_id": source_id} for p in PURPOSES]

    @app.post("/api/lean/templates", status_code=201)
    def l_template_create(b: EnvTemplateIn, p: Principal = Depends(need("plan:write"))):
        d = b.model_dump()
        if d["protect_after_build"] is None:
            d.pop("protect_after_build")
        return svc.lean.create_template(p, d).public()

    @app.get("/api/lean/templates")
    def l_templates(_: Principal = Depends(need("view"))):
        return [t.public() for t in svc.lean.templates.values()]

    @app.post("/api/lean/templates/{tid}/submit")
    def l_template_submit(tid: str, p: Principal = Depends(need("plan:submit"))):
        return svc.lean.submit_template(p, tid).public()

    @app.post("/api/lean/templates/{tid}/approve")
    def l_template_approve(tid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.lean.approve_template(p, tid).public()

    @app.post("/api/lean/templates/{tid}/estimate")
    def l_estimate(tid: str, b: EstimateIn, p: Principal = Depends(need("view"))):
        return svc.lean.estimate(p, tid, b.host_id)

    @app.post("/api/lean/builds", status_code=201)
    def l_build(b: BuildIn, p: Principal = Depends(need("client:build"))):
        return svc.lean.build(p, b.model_dump()).public()

    @app.get("/api/lean/builds")
    def l_builds(a: Principal = Depends(need("view"))):
        return [x.public() for x in reversed(list(svc.lean.builds.values())) if authz.visible(svc, a, x.host_id, x.source_id)]

    @app.get("/api/lean/builds/{bid}")
    def l_build_get(bid: str, _: Principal = Depends(need("view"))):
        return svc.lean.get_build(bid).public()

    @app.get("/api/lean/clients")
    def l_clients(a: Principal = Depends(need("view"))):
        return [c for c in svc.lean.clients() if authz.visible(svc, a, c["system_id"])]

    @app.post("/api/lean/clients/{system_id}/protection")
    def l_protect(system_id: str, b: ProtectIn, p: Principal = Depends(need("client:build"))):
        return svc.lean.set_protection(p, system_id, b.locked)

    @app.post("/api/lean/builds/{bid}/decommission")
    def l_decommission(bid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.lean.decommission(p, bid)

    @app.post("/api/lean/sweep")
    def l_sweep(p: Principal = Depends(need("run:execute"))):
        return svc.lean.sweep(p)

    # ---------------- post-copy automation factory ----------------
    @app.get("/api/postcopy/tasks")
    def pc_tasks(_: Principal = Depends(need("view"))):
        from ..postcopy.tasks import catalog
        return catalog()

    @app.get("/api/postcopy/systems/{sid}/tech")
    def pc_tech(sid: str, _: Principal = Depends(need("view"))):
        return {"system": svc.system(sid).label, "state": svc.postcopy.tech(sid).snapshot(), "simulated": True}

    @app.get("/api/postcopy/systems/{sid}/assessment")
    def pc_assess(sid: str, profile_id: str | None = None, _: Principal = Depends(need("view"))):
        return svc.postcopy.assess(sid, profile_id)

    @app.get("/api/postcopy/systems/{sid}/gate")
    def pc_gate(sid: str, profile_id: str | None = None, _: Principal = Depends(need("view"))):
        return svc.postcopy.gate(sid, profile_id)

    @app.post("/api/postcopy/profiles", status_code=201)
    def pc_capture(b: CaptureIn, p: Principal = Depends(need("plan:write"))):
        return svc.postcopy.capture_profile(p, b.system_id, b.name).public()

    @app.get("/api/postcopy/profiles")
    def pc_profiles(a: Principal = Depends(need("view"))):
        return [x.public() for x in svc.postcopy.profiles.values() if authz.visible(svc, a, x.system_id)]

    @app.post("/api/postcopy/profiles/{pid}/submit")
    def pc_profile_submit(pid: str, p: Principal = Depends(need("plan:submit"))):
        return svc.postcopy.submit_profile(p, pid).public()

    @app.post("/api/postcopy/profiles/{pid}/approve")
    def pc_profile_approve(pid: str, p: Principal = Depends(need("plan:approve"))):
        return svc.postcopy.approve_profile(p, pid).public()

    @app.post("/api/postcopy/runs", status_code=201)
    def pc_run_create(b: PcRunIn, p: Principal = Depends(need("plan:write"))):
        return svc.postcopy.create_run(p, b.model_dump()).public()

    @app.get("/api/postcopy/runs")
    def pc_runs(a: Principal = Depends(need("view"))):
        return [r.public() for r in reversed(list(svc.postcopy.runs.values())) if authz.visible(svc, a, r.target_id, r.source_id)]

    @app.get("/api/postcopy/runs/{rid}")
    def pc_run(rid: str, _: Principal = Depends(need("view"))):
        return svc.postcopy.get_run(rid).public()

    @app.post("/api/postcopy/runs/{rid}/approvals")
    def pc_approve(rid: str, b: LabelIn, p: Principal = Depends(need("view"))):
        return svc.postcopy.approve_run(p, rid, b.label).public()

    @app.post("/api/postcopy/runs/{rid}/execute")
    def pc_execute(rid: str, p: Principal = Depends(need("run:execute"))):
        return svc.postcopy.execute(p, rid).public()

    @app.post("/api/postcopy/runs/{rid}/resume")
    def pc_resume(rid: str, p: Principal = Depends(need("run:execute"))):
        return svc.postcopy.resume(p, rid).public()

    @app.post("/api/postcopy/runs/{rid}/rollback")
    def pc_rollback(rid: str, p: Principal = Depends(need("view"))):
        return svc.postcopy.rollback(p, rid).public()

    @app.get("/api/postcopy/runs/{rid}/evidence")
    def pc_evidence(rid: str, _: Principal = Depends(need("audit:read"))):
        return Response(svc.postcopy.evidence_package(rid), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{rid}-evidence.zip"'})

    @app.post("/api/demo/simulate-system-copy")
    def pc_simulate_copy(source_id: str, target_id: str, p: Principal = Depends(need("system:write"))):
        return svc.postcopy.simulate_copy(p, source_id, target_id)

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app


app = create_app()

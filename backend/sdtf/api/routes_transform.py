"""Scope, manifests, carve-out, rules, runs, audit, agents, cutover, platform."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..agents.catalog import agent_catalog
from ..agents.framework import REGISTRY, decide
from ..audit.service import record_event, verify_chain
from ..carveout.service import carveout_classification, completeness_report, residual_exposure_report
from ..catalog.store import RecordStore
from ..cutover.service import generate_runbook
from ..db import get_db
from ..models import (
    AgentDecision,
    ApprovalRecord,
    AuditEvent,
    MigrationRun,
    ReconciliationResult,
    RuleSet,
    SapSystem,
    ScopeManifest,
    TransformationException,
)
from ..rules.engine import dry_run, parse_ruleset, validate_ruleset
from ..rules.factory import generate_candidate_ruleset
from ..runtime.pipeline import RunPrecondition, resume_run, start_run
from ..scope.models import ScopeDefinition
from ..scope.service import (
    apply_disposition,
    approve_manifest,
    compare_manifests,
    create_manifest,
    evaluate_scope,
    pending_dispositions,
    reject_manifest,
)
from ..security.auth import Principal, assert_project_access, current_principal, require
from .deps import get_manifest, get_ruleset, get_run, manifest_out, ruleset_out, run_out

router = APIRouter()


# -------------------------------------------------------------------------------------------- scope
@router.post("/projects/{project_id}/scopes/evaluate", tags=["scope"])
def scope_evaluate(project_id: str, defn: ScopeDefinition, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    assert_project_access(db, p, project_id)
    ev = evaluate_scope(db, defn)
    by_cls = {}
    for n, c in ev["classification"].items():
        by_cls.setdefault(c["classification"], []).append(n)
    return {"impact": ev["impact"], "samples": {k: v[:20] for k, v in by_cls.items()}, "traces": ev["traces"][:200], "stopped": ev["stopped"][:50], "skipped": ev["skipped"][:50], "missing": ev["missing"][:50]}


@router.post("/projects/{project_id}/manifests", tags=["scope"], status_code=201)
def manifest_create(project_id: str, defn: ScopeDefinition, db: Session = Depends(get_db), p: Principal = Depends(require("scope:write"))):
    assert_project_access(db, p, project_id)
    m = create_manifest(db, project_id, defn, p.username)
    record_event(db, p.username, "MANIFEST_CREATED", "MANIFEST", m.id, {"version": m.version, "hash": m.content_hash})
    return manifest_out(m)


@router.get("/projects/{project_id}/manifests", tags=["scope"])
def manifest_list(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    assert_project_access(db, p, project_id)
    rows = db.execute(select(ScopeManifest).where(ScopeManifest.project_id == project_id).order_by(ScopeManifest.created_at.desc())).scalars().all()
    return [manifest_out(m) for m in rows]


@router.get("/manifests/{manifest_id}", tags=["scope"])
def manifest_get(full: bool = False, m: ScopeManifest = Depends(get_manifest), p: Principal = Depends(require("project:read"))):
    return manifest_out(m, full=full)


@router.get("/manifests/{manifest_id}/objects", tags=["scope"])
def manifest_objects(m: ScopeManifest = Depends(get_manifest), classification: str | None = None, object_type: str | None = None, requires_approval: bool | None = None, limit: int = Query(200, le=5000), p: Principal = Depends(require("project:read"))):
    cls = m.selection.get("classification", {})
    out = []
    for n, c in cls.items():
        if classification and c["classification"] != classification:
            continue
        if object_type and c["type"] != object_type:
            continue
        if requires_approval is not None and bool(c.get("requires_approval")) != requires_approval:
            continue
        out.append({"node": n, **c})
        if len(out) >= limit:
            break
    return {"total": len(cls), "items": out, "pending_dispositions": len(pending_dispositions(m))}


@router.get("/manifests/{manifest_id}/traces", tags=["scope"])
def manifest_traces(node: str | None = None, limit: int = Query(500, le=5000), m: ScopeManifest = Depends(get_manifest), p: Principal = Depends(require("project:read"))):
    traces = m.selection.get("traces", [])
    if node:
        traces = [t for t in traces if t["node"] == node or t.get("from") == node]
    return traces[:limit]


class DispositionRequest(BaseModel):
    nodes: list[str] = Field(default_factory=list)
    all_pending: bool = False
    decision: str
    comment: str = ""


@router.post("/manifests/{manifest_id}/dispositions", tags=["scope"])
def manifest_disposition(req: DispositionRequest, m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("approve:manifest"))):
    nodes = pending_dispositions(m) if req.all_pending else req.nodes
    n = apply_disposition(db, m, nodes, req.decision, p.username, req.comment)
    record_event(db, p.username, "MANIFEST_DISPOSITIONED", "MANIFEST", m.id, {"decision": req.decision, "count": n, "hash": m.content_hash})
    return {"updated": n, "pending": len(pending_dispositions(m)), "content_hash": m.content_hash, "impact": m.impact}


class ApprovalRequest(BaseModel):
    comment: str = ""


@router.post("/manifests/{manifest_id}/approve", tags=["scope"])
def manifest_approve(req: ApprovalRequest, m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("approve:manifest"))):
    return manifest_out(approve_manifest(db, m, p.username, req.comment))


@router.post("/manifests/{manifest_id}/reject", tags=["scope"])
def manifest_reject(req: ApprovalRequest, m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("approve:manifest"))):
    return manifest_out(reject_manifest(db, m, p.username, req.comment))


@router.get("/manifests/{manifest_id}/compare/{other_id}", tags=["scope"])
def manifest_compare(other_id: str, m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    o = db.get(ScopeManifest, other_id)
    if o is None or o.project_id != m.project_id:
        raise HTTPException(404, "other manifest not found in project")
    return compare_manifests(m, o)


# ----------------------------------------------------------------------------------------- carve-out
@router.get("/manifests/{manifest_id}/carveout/classification", tags=["carveout"])
def carveout_cls(m: ScopeManifest = Depends(get_manifest), p: Principal = Depends(require("project:read"))):
    return carveout_classification(m)


@router.get("/manifests/{manifest_id}/carveout/completeness", tags=["carveout"])
def carveout_completeness(m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    return completeness_report(db, m)


@router.get("/manifests/{manifest_id}/carveout/residual", tags=["carveout"])
def carveout_residual(m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    return residual_exposure_report(db, m)


# --------------------------------------------------------------------------------------------- rules
class RuleSetCreate(BaseModel):
    source_yaml: str


@router.post("/projects/{project_id}/rulesets", tags=["rules"], status_code=201)
def ruleset_create(project_id: str, req: RuleSetCreate, db: Session = Depends(get_db), p: Principal = Depends(require("rules:write"))):
    assert_project_access(db, p, project_id)
    try:
        rs = parse_ruleset(req.source_yaml)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"invalid ruleset: {e}") from None
    validation = validate_ruleset(rs)
    version = (db.execute(select(func.max(RuleSet.version)).where(RuleSet.project_id == project_id, RuleSet.name == rs.name)).scalar() or 0) + 1
    row = RuleSet(project_id=project_id, name=rs.name, version=version, content_hash=rs.content_hash, source_yaml=req.source_yaml, compiled={"rules": rs.rules, "lookups": rs.lookups, "applies_to": rs.applies_to}, validation=validation, created_by=p.username)
    db.add(row)
    db.flush()
    record_event(db, p.username, "RULESET_CREATED", "RULESET", row.id, {"name": rs.name, "version": version, "valid": validation["ok"]})
    return ruleset_out(row, full=True)


class GenerateOptions(BaseModel):
    source_index: int = Field(0, ge=0, le=20, description="position of the source in a merge group; >0 selects disjoint number ranges and key prefixes")
    dedup: dict = Field(default_factory=dict, description="lookups from /merge/dedup: {customers|vendors|materials: {dup_key: survivor_key}}")
    coa_map: dict = Field(default_factory=dict)


@router.post("/projects/{project_id}/rulesets/generate", tags=["rules"])
def ruleset_generate(project_id: str, manifest_id: str, opts: GenerateOptions | None = None, db: Session = Depends(get_db), p: Principal = Depends(require("rules:write"))):
    assert_project_access(db, p, project_id)
    m = db.get(ScopeManifest, manifest_id)
    if m is None or m.project_id != project_id:
        raise HTTPException(404, "manifest not found")
    defn = ScopeDefinition(**m.definition)
    tgt = db.get(SapSystem, defn.target_system_id)
    opts = opts or GenerateOptions()
    return {"source_yaml": generate_candidate_ruleset(defn, tgt.product if tgt else "S4HANA", source_index=opts.source_index, dedup=opts.dedup or None, coa_map=opts.coa_map or None)}


@router.post("/rulesets/validate", tags=["rules"])
def ruleset_validate(req: RuleSetCreate, p: Principal = Depends(require("project:read"))):
    try:
        rs = parse_ruleset(req.source_yaml)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errors": [str(e)], "warnings": [], "tests": {"passed": 0, "failed": 0, "results": []}, "rule_count": 0}
    return validate_ruleset(rs)


@router.get("/projects/{project_id}/rulesets", tags=["rules"])
def ruleset_list(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    assert_project_access(db, p, project_id)
    return [ruleset_out(r) for r in db.execute(select(RuleSet).where(RuleSet.project_id == project_id).order_by(RuleSet.created_at.desc())).scalars().all()]


@router.get("/rulesets/{ruleset_id}", tags=["rules"])
def ruleset_get(r: RuleSet = Depends(get_ruleset), p: Principal = Depends(require("project:read"))):
    return ruleset_out(r, full=True)


class DryRunRequest(BaseModel):
    system_id: str
    tables: list[str] = Field(default_factory=lambda: ["BKPF", "BSEG", "KNA1", "VBAK", "EKKO", "MARC"])
    bukrs: str | None = None
    sample_size: int = Field(50, le=500)


@router.post("/rulesets/{ruleset_id}/dry-run", tags=["rules"])
def ruleset_dry_run(req: DryRunRequest, r: RuleSet = Depends(get_ruleset), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    s = db.get(SapSystem, req.system_id)
    if s is None or s.project_id != r.project_id:
        raise HTTPException(404, "system not found in project")
    store = RecordStore.load(db, s.id, tables=req.tables)
    samples = {}
    for t in req.tables:
        rows = store.rows(t)
        if req.bukrs:
            rows = [x for x in rows if x.get("BUKRS") == req.bukrs or x.get("BUKRS_VF") == req.bukrs]
        samples[t] = rows[: req.sample_size]
    return dry_run(parse_ruleset(r.source_yaml), samples)


@router.post("/rulesets/{ruleset_id}/approve", tags=["rules"])
def ruleset_approve(req: ApprovalRequest, r: RuleSet = Depends(get_ruleset), db: Session = Depends(get_db), p: Principal = Depends(require("approve:rules"))):
    from ..demo import approve_ruleset

    return ruleset_out(approve_ruleset(db, r, p.username, req.comment))


# ---------------------------------------------------------------------------------------------- runs
class RunRequest(BaseModel):
    manifest_id: str
    ruleset_id: str
    mode: str = "SIMULATED"
    workers: int | None = Field(None, ge=1, le=32)
    execution: str = Field("INLINE", pattern="^(INLINE|DISTRIBUTED)$", description="INLINE: threads in the API process; DISTRIBUTED: partition jobs claimed by `sdtf worker` processes")
    staging_backend: str | None = Field(None, pattern="^(relational|columnar)$")
    pipelined: bool = Field(True, description="DISTRIBUTED only: partitions flow through stages independently (default) or wait at stage barriers")


@router.post("/projects/{project_id}/runs", tags=["runs"], status_code=201)
def run_start(project_id: str, req: RunRequest, db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    assert_project_access(db, p, project_id)
    try:
        run = start_run(db, project_id, req.manifest_id, req.ruleset_id, p.username, req.mode, req.workers, execution=req.execution, staging_backend=req.staging_backend, pipelined=req.pipelined)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None
    return run_out(run)


@router.post("/runs/{run_id}/resume", tags=["runs"])
def run_resume(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    try:
        return run_out(resume_run(db, r.id, p.username))
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


@router.get("/projects/{project_id}/runs", tags=["runs"])
def run_list(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    assert_project_access(db, p, project_id)
    return [run_out(r) for r in db.execute(select(MigrationRun).where(MigrationRun.project_id == project_id).order_by(MigrationRun.created_at.desc())).scalars().all()]


@router.get("/runs/{run_id}", tags=["runs"])
def run_get(full: bool = False, r: MigrationRun = Depends(get_run), p: Principal = Depends(require("project:read"))):
    return run_out(r, full=full)


@router.get("/runs/{run_id}/jobs", tags=["runs"])
def run_jobs(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..runtime.worker import job_summary

    return job_summary(db, r.id)


@router.post("/runs/{run_id}/jobs/requeue", tags=["runs"])
def run_jobs_requeue(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Re-queue FAILED jobs and expired leases of a distributed run."""
    from ..runtime.worker import requeue_jobs, requeue_stale

    n = requeue_jobs(db, r.id, statuses=("FAILED",)) + requeue_stale(db)
    record_event(db, p.username, "JOBS_REQUEUED", "RUN", r.id, {"count": n})
    return {"requeued": n}


@router.get("/platform/workers", tags=["platform"])
def platform_workers(db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..models import ExtractionJob

    rows = db.execute(select(ExtractionJob.worker_id, ExtractionJob.status, func.count()).where(ExtractionJob.worker_id.isnot(None)).group_by(ExtractionJob.worker_id, ExtractionJob.status)).all()
    out: dict[str, dict] = {}
    for w, st, n in rows:
        out.setdefault(w, {"worker": w, "DONE": 0, "CLAIMED": 0, "FAILED": 0})[st] = n
    queued = db.execute(select(func.count()).select_from(ExtractionJob).where(ExtractionJob.status == "QUEUED")).scalar()
    return {"queued_jobs": queued, "workers": sorted(out.values(), key=lambda x: x["worker"])}


@router.get("/runs/{run_id}/report", tags=["runs"])
def run_report(r: MigrationRun = Depends(get_run), p: Principal = Depends(require("project:read"))):
    return r.report or {}


@router.get("/runs/{run_id}/reconciliation", tags=["runs"])
def run_reconciliation(layer: str | None = None, status: str | None = None, limit: int = Query(500, le=5000), r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    stmt = select(ReconciliationResult).where(ReconciliationResult.run_id == r.id)
    if layer:
        stmt = stmt.where(ReconciliationResult.layer == layer)
    if status:
        stmt = stmt.where(ReconciliationResult.status == status)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [{"id": x.id, "layer": x.layer, "check": x.check_name, "subject": x.subject, "status": x.status, "source": x.source_value, "target": x.target_value, "variance": x.variance, "explanation": x.explanation, "evidence": x.evidence} for x in rows]


@router.get("/runs/{run_id}/exceptions", tags=["runs"])
def run_exceptions(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    rows = db.execute(select(TransformationException).where(TransformationException.run_id == r.id).limit(1000)).scalars().all()
    return [{"id": x.id, "stage": x.stage, "table": x.table_name, "key": x.record_key, "rule": x.rule_id, "severity": x.severity, "message": x.message, "disposition": x.disposition} for x in rows]


@router.get("/runs/{run_id}/staged", tags=["runs"])
def run_staged(table: str | None = None, limit: int = Query(50, le=500), r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("records:read"))):
    from ..staging import get_backend

    backend = get_backend(r.metrics.get("staging_backend"), session=db)
    rows = backend.samples(r.id, table, limit)
    return {"backend": backend.name, "counts": backend.counts(r.id), "items": [{"table": x.table_name, "key": x.record_key, "target_key": x.target_key, "status": x.load_status, "lineage": x.lineage, "source": x.source_payload, "target": x.target_payload} for x in rows]}


class RunSignoff(BaseModel):
    kind: str = Field(pattern="^(TECHNICAL|BUSINESS)$")
    decision: str = Field(pattern="^(APPROVED|REJECTED)$")
    comment: str = ""


@router.post("/runs/{run_id}/signoff", tags=["runs"])
def run_signoff(req: RunSignoff, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("approve:run"))):
    db.add(ApprovalRecord(subject_type="RECONCILIATION", subject_id=r.id, decision=req.decision, decided_by=p.username, kind=req.kind, comment=req.comment))
    record_event(db, p.username, "RUN_SIGNOFF", "RUN", r.id, {"kind": req.kind, "decision": req.decision})
    return {"ok": True}


# --------------------------------------------------------------------------------------------- audit
@router.get("/audit/events", tags=["audit"])
def audit_events(subject_type: str | None = None, subject_id: str | None = None, limit: int = Query(200, le=2000), db: Session = Depends(get_db), p: Principal = Depends(require("audit:read"))):
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == p.tenant_id)
    if subject_type:
        stmt = stmt.where(AuditEvent.subject_type == subject_type)
    if subject_id:
        stmt = stmt.where(AuditEvent.subject_id == subject_id)
    rows = db.execute(stmt.order_by(AuditEvent.id.desc()).limit(limit)).scalars().all()
    return [{"id": e.id, "ts": e.ts, "actor": e.actor, "action": e.action, "subject_type": e.subject_type, "subject_id": e.subject_id, "details": e.details, "hash": e.hash, "prev_hash": e.prev_hash} for e in rows]


@router.get("/audit/verify", tags=["audit"])
def audit_verify(db: Session = Depends(get_db), p: Principal = Depends(require("audit:read"))):
    return verify_chain(db)


@router.get("/audit/approvals", tags=["audit"])
def approvals(subject_id: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    stmt = select(ApprovalRecord)
    if subject_id:
        stmt = stmt.where(ApprovalRecord.subject_id == subject_id)
    return [{"id": a.id, "subject_type": a.subject_type, "subject_id": a.subject_id, "decision": a.decision, "decided_by": a.decided_by, "kind": a.kind, "comment": a.comment, "created_at": a.created_at} for a in db.execute(stmt.order_by(ApprovalRecord.created_at.desc()).limit(500)).scalars().all()]


@router.get("/runs/{run_id}/evidence", tags=["audit"])
def run_evidence(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    ev = (r.report or {}).get("evidence", {})
    events = db.execute(select(AuditEvent).where(AuditEvent.subject_id == r.id).order_by(AuditEvent.id)).scalars().all()
    appr = db.execute(select(ApprovalRecord).where(ApprovalRecord.subject_id.in_([r.id, r.manifest_id, r.ruleset_id]))).scalars().all()
    return {"evidence": ev, "audit_events": [{"ts": e.ts, "actor": e.actor, "action": e.action, "hash": e.hash} for e in events], "approvals": [{"subject_type": a.subject_type, "subject_id": a.subject_id, "decision": a.decision, "decided_by": a.decided_by, "kind": a.kind} for a in appr], "markdown": (r.report or {}).get("markdown", "")}


# -------------------------------------------------------------------------------------------- agents
@router.get("/agents", tags=["agents"])
def agents(p: Principal = Depends(current_principal)):
    return agent_catalog()


class AgentRunRequest(BaseModel):
    context: dict = Field(default_factory=dict)


def _decision_out(d: AgentDecision) -> dict:
    return {"id": d.id, "project_id": d.project_id, "agent": d.agent, "subject_type": d.subject_type, "subject_id": d.subject_id, "proposal": d.proposal, "confidence": d.confidence, "evidence": d.evidence, "status": d.status, "decided_by": d.decided_by, "requested_by": d.requested_by, "created_at": d.created_at}


@router.post("/projects/{project_id}/agents/{agent_name}/run", tags=["agents"])
def agent_run(project_id: str, agent_name: str, req: AgentRunRequest, db: Session = Depends(get_db), p: Principal = Depends(require("agent:run"))):
    assert_project_access(db, p, project_id)
    cls = REGISTRY.get(agent_name)
    if cls is None:
        raise HTTPException(404, "unknown agent")
    try:
        d = cls().run(db, project_id, req.context, p.username)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    record_event(db, p.username, "AGENT_PROPOSAL", "AGENT_DECISION", d.id, {"agent": agent_name, "confidence": d.confidence})
    return _decision_out(d)


@router.get("/projects/{project_id}/agents/decisions", tags=["agents"])
def agent_decisions(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    assert_project_access(db, p, project_id)
    return [_decision_out(d) for d in db.execute(select(AgentDecision).where(AgentDecision.project_id == project_id).order_by(AgentDecision.created_at.desc()).limit(200)).scalars().all()]


class DecideRequest(BaseModel):
    accept: bool


@router.post("/agents/decisions/{decision_id}/decide", tags=["agents"])
def agent_decide(decision_id: str, req: DecideRequest, db: Session = Depends(get_db), p: Principal = Depends(require("agent:decide"))):
    d = db.get(AgentDecision, decision_id)
    if d is None:
        raise HTTPException(404, "decision not found")
    assert_project_access(db, p, d.project_id)
    decide(db, d, req.accept, p.username)
    record_event(db, p.username, "AGENT_DECISION", "AGENT_DECISION", d.id, {"accepted": req.accept})
    return _decision_out(d)


# ------------------------------------------------------------------------------------------- cutover
@router.get("/manifests/{manifest_id}/cutover/runbook", tags=["cutover"])
def cutover_runbook(m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    return generate_runbook(db, m)


# ------------------------------------------------------------------------------------------ platform
CAPABILITIES = [
    {"area": "Synthetic ECC landscape", "status": "IMPLEMENTED", "note": "Deterministic generator with shared masters, cross-company documents, balanced FI"},
    {"area": "Landscape discovery", "status": "IMPLEMENTED", "note": "Runs against the record store; SAP connectors are planned"},
    {"area": "Business object dependency graph", "status": "IMPLEMENTED", "note": "Relational store by default; Neo4j property-graph adapter (SDTF_GRAPH_BACKEND=neo4j) verified at Cypher level, not against a live server here"},
    {"area": "Selective scope designer + manifest", "status": "IMPLEMENTED", "note": "Versioned, hashed, four-eyes approval"},
    {"area": "Carve-out classification & reports", "status": "IMPLEMENTED", "note": "Completeness, residual exposure, intercompany balances"},
    {"area": "Transformation rule DSL", "status": "IMPLEMENTED", "note": "YAML DSL, validation, embedded tests, dry run"},
    {"area": "Extraction", "status": "IMPLEMENTED", "note": "Synthetic store extractor and RFC adapter against the ABAP add-on contract (pushdown, snapshot token, checksums); RFC verified on the simulated add-on only, not on a live SAP system; OData/CDS adapters planned"},
    {"area": "Distributed extraction workers", "status": "IMPLEMENTED", "note": "Claim-based partition jobs with leases, crash re-queue, last-worker finalisation; `sdtf worker` processes / pods"},
    {"area": "Columnar staging (Parquet on local / S3 / GCS / Azure via fsspec, key-range sidecar index)", "status": "IMPLEMENTED", "note": "Per run/table/partition files, zstd; object-store path tested with the in-memory filesystem"},
    {"area": "Observability (OpenTelemetry traces, metrics, trace-correlated JSON logs)", "status": "IMPLEMENTED", "note": "OTLP/HTTP export when OTEL_EXPORTER_OTLP_ENDPOINT is set; no-op otherwise"},
    {"area": "Target load", "status": "SIMULATED", "note": "Initial load: simulated loader with idempotent upsert tagged with the registry's load method. Delta loads: released S/4HANA APIs (business partner, product, sales/purchase order, delivery, journal entry) through the API connector, verified on the simulated gateway only (ADR-0015)"},
    {"area": "Reconciliation (technical/functional/financial)", "status": "IMPLEMENTED", "note": "Runs on simulated data"},
    {"area": "Audit trail & evidence packages", "status": "IMPLEMENTED", "note": "Hash-chained events, evidence index"},
    {"area": "AI agents", "status": "IMPLEMENTED", "note": "12 bounded heuristic agents; LLM reasoner planned"},
    {"area": "Multi-source merger / consolidation", "status": "IMPLEMENTED", "note": "Merge groups, cross-system key collision planning, master-data dedup, group-level financial reconciliation (simulated runtime)"},
    {"area": "Delta capture / near-zero downtime", "status": "SIMULATED", "note": "CDC through the SAP add-on contract (Z_SDTF_CDC_POLL) over RFC, ordered idempotent replay, freeze, final delta + full reconciliation; verified on the simulated add-on only, no downtime figure claimed (ADR-0014)"},
    {"area": "Cutover command center", "status": "PARTIAL", "note": "Runbook generation, critical path, forecast; execution tracking planned"},
    {"area": "SSO / enterprise identity", "status": "IMPLEMENTED", "note": "OIDC RS256 bearer tokens verified against JWKS with group-to-role mapping; dev users remain for local use"},
    {"area": "Production SAP migration", "status": "UNSUPPORTED", "note": "This build never connects to or writes into an SAP system"},
]


@router.get("/platform/telemetry", tags=["platform"])
def platform_telemetry(p: Principal = Depends(current_principal)):
    from .. import observability as obs

    return obs.status()


@router.get("/platform/capabilities", tags=["platform"])
def capabilities(p: Principal = Depends(current_principal)):
    return CAPABILITIES


@router.get("/platform/portfolio", tags=["platform"])
def portfolio(db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..models import Project

    projects = db.execute(select(Project).where(Project.tenant_id == p.tenant_id)).scalars().all()
    out = []
    for proj in projects:
        manifests = db.execute(select(ScopeManifest).where(ScopeManifest.project_id == proj.id)).scalars().all()
        runs = db.execute(select(MigrationRun).where(MigrationRun.project_id == proj.id)).scalars().all()
        out.append({"id": proj.id, "name": proj.name, "scenario_type": proj.scenario_type, "status": proj.status, "manifests": len(manifests), "approved_manifests": sum(1 for m in manifests if m.status == "APPROVED"), "runs": len(runs), "completed_runs": sum(1 for r in runs if r.status == "COMPLETED"), "last_reconciliation": next(((r.report or {}).get("reconciliation", {}).get("overall") for r in sorted(runs, key=lambda r: r.created_at, reverse=True) if r.report), None), "objects_in_scope": max((m.impact.get("objects_total", 0) for m in manifests), default=0)})
    return out


@router.get("/platform/delta/status", tags=["platform"])
def delta_status(p: Principal = Depends(current_principal)):
    stages = ["Initial extraction", "Initial transformation", "Initial target load", "Delta capture", "Delta transformation", "Continuous synchronization", "Backlog monitoring", "Business freeze coordination", "Final delta synchronization", "Final reconciliation", "Cutover authorization", "Business validation", "Production handover"]
    status = {s: "SIMULATED" for s in stages[:10]}
    status["Cutover authorization"] = "PARTIAL"
    status["Business validation"] = "PLANNED"
    status["Production handover"] = "PLANNED"
    return {"status": "SIMULATED", "stages": stages, "implemented_stages": stages[:10], "stage_status": status, "note": "Delta capture runs through the SAP add-on contract (Z_SDTF_CDC_POLL) over the RFC adapter and is verified on the simulated add-on only; apply targets the simulated record store. Cutover authorization is the runbook go/no-go gate; business validation and production handover are human stages. No downtime figure is derived from simulated cycles. See docs/07-ndt-cdc-consistency-recovery.md and ADR-0014."}


# ------------------------------------------------------------------------------------ delta sync
class DeltaCycleRequest(BaseModel):
    final: bool = False


class FreezeRequest(BaseModel):
    note: str = ""


@router.get("/runs/{run_id}/delta", tags=["delta"])
def delta_get(backlog: bool = True, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..runtime.delta import delta_state

    base = db.get(MigrationRun, r.metrics["baseline_run_id"]) if r.metrics.get("kind") == "DELTA" else r
    return delta_state(db, base, with_backlog=backlog)


@router.post("/runs/{run_id}/delta/cycles", tags=["delta"], status_code=201)
def delta_cycle(req: DeltaCycleRequest, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..runtime.delta import start_delta_cycle

    try:
        return run_out(start_delta_cycle(db, r.id, p.username, final=req.final), full=True)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


@router.post("/runs/{run_id}/delta/freeze", tags=["delta"])
def delta_freeze(req: FreezeRequest, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("approve:run"))):
    from ..runtime.delta import declare_freeze

    if r.metrics.get("kind") == "DELTA":
        raise HTTPException(409, "declare the freeze on the baseline run")
    try:
        return declare_freeze(db, r, p.username, req.note)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


@router.get("/runs/{run_id}/delta/events", tags=["delta"])
def delta_events(status: str | None = None, limit: int = Query(200, ge=1, le=2000), r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..models import DeltaEvent

    col = DeltaEvent.run_id if r.metrics.get("kind") == "DELTA" else DeltaEvent.baseline_run_id
    stmt = select(DeltaEvent).where(col == r.id)
    if status:
        stmt = stmt.where(DeltaEvent.status == status)
    rows = db.execute(stmt.order_by(DeltaEvent.seq.desc()).limit(limit)).scalars().all()
    return [{"id": e.id, "cycle_run_id": e.run_id, "seq": e.seq, "changenr": e.changenr, "object_type": e.object_type, "object_key": e.object_key, "table": e.table_name, "record_key": e.record_key, "op": e.op, "changed_at": e.changed_at, "changed_by": e.changed_by, "status": e.status, "action": e.action, "load_method": e.load_method, "api_call": e.api_call, "target_key": e.target_key, "message": e.message} for e in rows]


# ------------------------------------------------------------------------------------------ merger
class MergeSource(BaseModel):
    manifest_id: str
    ruleset_id: str


class MergeRequest(BaseModel):
    sources: list[MergeSource] = Field(min_length=2)


@router.post("/projects/{project_id}/merge/plan", tags=["merger"])
def merge_plan(project_id: str, req: MergeRequest, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..runtime.merge import plan_merge

    assert_project_access(db, p, project_id)
    try:
        return plan_merge(db, project_id, [s.model_dump() for s in req.sources])
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


@router.post("/projects/{project_id}/merge/run", tags=["merger"], status_code=201)
def merge_run(project_id: str, req: MergeRequest, db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..runtime.merge import start_merge_run

    assert_project_access(db, p, project_id)
    try:
        return start_merge_run(db, project_id, [s.model_dump() for s in req.sources], p.username)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


class DedupRequest(BaseModel):
    leading_manifest_id: str
    leading_ruleset_id: str
    source_system_id: str


@router.post("/projects/{project_id}/merge/dedup", tags=["merger"])
def merge_dedup(project_id: str, req: DedupRequest, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """Duplicate master-data candidates of a non-leading source and the lookups the rule factory needs."""
    from ..runtime.merge import dedup_for_merge

    assert_project_access(db, p, project_id)
    try:
        return dedup_for_merge(db, project_id, {"manifest_id": req.leading_manifest_id, "ruleset_id": req.leading_ruleset_id}, req.source_system_id)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None


@router.get("/projects/{project_id}/merge/duplicates", tags=["merger"])
def merge_duplicates(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..catalog.dedup import find_duplicate_masters

    assert_project_access(db, p, project_id)
    systems = [s.id for s in db.execute(select(SapSystem).where(SapSystem.project_id == project_id, SapSystem.role == "SOURCE")).scalars()]
    if len(systems) < 2:
        return {"systems": systems, "candidates": {}, "note": "at least two source systems are needed"}
    cands = find_duplicate_masters(db, systems)
    return {"systems": systems, "counts": {k: len(v) for k, v in cands.items()}, "candidates": {k: v[:100] for k, v in cands.items()}}

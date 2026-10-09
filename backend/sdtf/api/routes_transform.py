"""Scope, manifests, carve-out, rules, runs, audit, agents, cutover, platform."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
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
    load_mode: str | None = Field(None, pattern="^(api|direct)$", description="api: released-API loaders over the target transport (default); direct: simulated direct loader")


@router.post("/projects/{project_id}/runs", tags=["runs"], status_code=201)
def run_start(project_id: str, req: RunRequest, db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    assert_project_access(db, p, project_id)
    try:
        run = start_run(db, project_id, req.manifest_id, req.ruleset_id, p.username, req.mode, req.workers, execution=req.execution, staging_backend=req.staging_backend, pipelined=req.pipelined, load_mode=req.load_mode)
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


@router.post("/runs/{run_id}/reconcile", tags=["runs"])
def run_reconcile_again(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Re-run the three-layer reconciliation of a completed run through the adapters (source over the RFC add-on,
    target over the released APIs; record-store systems read directly) and replace its results and report."""
    from ..reconciliation.views import ReconciliationViewError
    from ..runtime.pipeline import reconcile_again
    from ..runtime.rfc import RfcError
    from ..runtime.target_api import ApiError

    try:
        return reconcile_again(db, r.id, p.username)
    except RunPrecondition as e:
        raise HTTPException(409, str(e)) from None
    except (ReconciliationViewError, RfcError, ApiError) as e:
        raise HTTPException(502, f"reconciliation read failed: {e}") from None


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


class CockpitExportRequest(BaseModel):
    formats: list[str] = Field(["csv", "xml"], description="csv: one file per staging table; xml: one SpreadsheetML workbook per migration object")
    use_templates: bool = Field(True, description="also fill the project's registered migration object templates (<OBJECT>.template.xml)")
    scope: str = Field("all", pattern="^(all|rejected)$", description="rejected: retry package of the instances the imported upload simulation feedback rejected")


@router.post("/runs/{run_id}/cockpit-export", tags=["runs"], status_code=201)
def run_cockpit_export(req: CockpitExportRequest | None = None, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Write the migration cockpit staging-file package for this run (the rows the initial load routes to the
    cockpit: cockpit objects, tables the document APIs do not expose, histories) and return its summary."""
    from ..runtime.cockpit_export import export_cockpit_files

    formats = tuple(f for f in (req.formats if req else ["csv", "xml"]) if f in ("csv", "xml"))
    if not formats:
        raise HTTPException(422, "formats must include csv and/or xml")
    if r.status not in ("COMPLETED", "FAILED"):
        raise HTTPException(409, f"run is {r.status}; export after the TRANSFORM stage has finished")
    return export_cockpit_files(db, r.id, actor=p.username, formats=formats, use_templates=req.use_templates if req else True, scope=req.scope if req else "all")


@router.get("/runs/{run_id}/cockpit-export", tags=["runs"])
def run_cockpit_export_get(r: MigrationRun = Depends(get_run), p: Principal = Depends(require("project:read"))):
    from ..runtime.cockpit_export import cockpit_export_summary

    s = cockpit_export_summary(r)
    if s is None:
        return {"exported": False, "run_id": r.id}
    return {"exported": True, **s}


@router.get("/runs/{run_id}/cockpit-export/download", tags=["runs"])
def run_cockpit_export_download(r: MigrationRun = Depends(get_run), p: Principal = Depends(require("project:read"))):
    import os

    from ..runtime.cockpit_export import cockpit_export_summary

    s = cockpit_export_summary(r)
    if s is None or not os.path.isfile(s["zip"]):
        raise HTTPException(404, "no cockpit export package on disk for this run; export it first")
    return FileResponse(s["zip"], media_type="application/zip", filename=os.path.basename(s["zip"]))


class CockpitTemplateIn(BaseModel):
    object_type: str
    filename: str = "template.xml"
    content: str = Field(..., description="the migration object template as the Migrate Your Data app exports it (SpreadsheetML 2003 XML)")
    mapping: dict | None = Field(None, description='explicit overrides: {"<sheet>": {"table": "VBRP", "fields": {"FIELD": "VBRP.NETWR" | "=const" | ""}}}')


class CockpitMappingIn(BaseModel):
    mapping: dict


@router.get("/cockpit-templates/samples", tags=["runs"])
def cockpit_template_samples(p: Principal = Depends(require("project:read"))):
    from ..runtime.cockpit_templates import SAMPLE_OBJECTS

    return {"objects": list(SAMPLE_OBJECTS), "note": "illustrative templates in the layout of the app's XML templates; not SAP files"}


@router.get("/cockpit-templates/samples/{object_type}", tags=["runs"])
def cockpit_template_sample(object_type: str, p: Principal = Depends(require("project:read"))):
    from ..runtime.cockpit_templates import sample_template

    try:
        xml = sample_template(object_type)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
    return Response(xml, media_type="application/xml", headers={"Content-Disposition": f'attachment; filename="{object_type}.sample-template.xml"'})


class CockpitTemplateCheckIn(BaseModel):
    content: str
    object_type: str | None = None


@router.post("/cockpit-templates/check", tags=["runs"])
def cockpit_template_check(req: CockpitTemplateCheckIn, p: Principal = Depends(require("project:read"))):
    """Check a downloaded template against the documented layout without storing it; with `object_type` the
    automatic mapping report is included."""
    from ..runtime.cockpit_templates import auto_map, check_template, mapping_report, parse_template

    chk = check_template(req.content)
    rep = mapping_report(auto_map(parse_template(req.content), req.object_type)) if chk["ok"] and req.object_type else None
    return {"check": chk, "mapping": rep}


@router.post("/projects/{project_id}/cockpit-templates", tags=["runs"], status_code=201)
def cockpit_template_register(project_id: str, req: CockpitTemplateIn, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Register (or replace) the migration object template of one business object for the project; the response
    carries the parsed structure and the automatic mapping report (coverage, mandatory fields left unmapped)."""
    from ..runtime.cockpit_templates import (
        project_aliases,
        register_template,
        store_proposals,
        template_summary,
    )

    assert_project_access(db, p, project_id)
    try:
        row = register_template(db, project_id, req.object_type, req.content, req.filename, p.username, req.mapping)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    store_proposals(db, project_id, row, p.username)
    return template_summary(row, project_aliases(db, project_id))


@router.get("/projects/{project_id}/cockpit-templates", tags=["runs"])
def cockpit_template_list(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..runtime.cockpit_templates import project_aliases, template_summary, templates_for

    assert_project_access(db, p, project_id)
    al = project_aliases(db, project_id)
    return [template_summary(t, al) for _, t in sorted(templates_for(db, project_id).items())]


def _template(db: Session, p: Principal, project_id: str, template_id: str):
    from ..models import CockpitTemplate

    assert_project_access(db, p, project_id)
    t = db.get(CockpitTemplate, template_id)
    if t is None or t.project_id != project_id:
        raise HTTPException(404, "template not found")
    return t


@router.get("/projects/{project_id}/cockpit-templates/{template_id}", tags=["runs"])
def cockpit_template_get(project_id: str, template_id: str, content: bool = False, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..runtime.cockpit_templates import project_aliases, template_summary

    t = _template(db, p, project_id, template_id)
    out = {**template_summary(t, project_aliases(db, project_id)), "structure": t.structure}
    if content:
        out["content"] = t.content
    return out


@router.put("/projects/{project_id}/cockpit-templates/{template_id}/mapping", tags=["runs"])
def cockpit_template_mapping(project_id: str, template_id: str, req: CockpitMappingIn, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..runtime.cockpit_templates import project_aliases, template_summary

    t = _template(db, p, project_id, template_id)
    t.mapping = req.mapping
    db.flush()
    record_event(db, p.username, "COCKPIT_TEMPLATE_MAPPED", "PROJECT", project_id, {"object_type": t.object_type, "sheets": sorted(req.mapping)})
    return template_summary(t, project_aliases(db, project_id))


@router.delete("/projects/{project_id}/cockpit-templates/{template_id}", tags=["runs"], status_code=204)
def cockpit_template_delete(project_id: str, template_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    t = _template(db, p, project_id, template_id)
    record_event(db, p.username, "COCKPIT_TEMPLATE_DELETED", "PROJECT", project_id, {"object_type": t.object_type})
    db.delete(t)
    db.flush()
    return Response(status_code=204)


class CockpitAliasIn(BaseModel):
    alias: str
    field: str
    table: str = Field("", description="restrict the alias to one table; empty = any table carrying the field")
    description: str = ""


class CockpitAliasDecision(BaseModel):
    status: str = Field(..., pattern="^(CONFIRMED|REJECTED)$")
    field: str | None = Field(None, description="correct the target field when confirming")


@router.get("/cockpit-aliases/catalogue", tags=["runs"])
def cockpit_alias_catalogue(p: Principal = Depends(require("project:read"))):
    """The global alias catalogue (template / BAPI field names -> DDIC fields) and the DDIC field descriptions."""
    from ..catalog.fields import FIELD_DESCRIPTIONS, TEMPLATE_ALIASES

    return {"aliases": TEMPLATE_ALIASES, "descriptions": FIELD_DESCRIPTIONS, "count": len(TEMPLATE_ALIASES)}


@router.get("/projects/{project_id}/cockpit-aliases", tags=["runs"])
def cockpit_alias_list(project_id: str, status: str | None = None, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..models import CockpitAlias
    from ..runtime.cockpit_templates import alias_out

    assert_project_access(db, p, project_id)
    stmt = select(CockpitAlias).where(CockpitAlias.project_id == project_id)
    if status:
        stmt = stmt.where(CockpitAlias.status == status.upper())
    return [alias_out(a) for a in db.execute(stmt.order_by(CockpitAlias.status, CockpitAlias.alias)).scalars().all()]


@router.post("/projects/{project_id}/cockpit-aliases", tags=["runs"], status_code=201)
def cockpit_alias_add(project_id: str, req: CockpitAliasIn, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Add (and confirm) an alias by hand."""
    from ..catalog.fields import FIELD_DESCRIPTIONS
    from ..catalog.tables import TABLES
    from ..models import CockpitAlias
    from ..runtime.cockpit_templates import alias_out, decide_alias

    assert_project_access(db, p, project_id)
    fld, tbl = req.field.upper(), req.table.upper()
    if tbl and tbl not in TABLES:
        raise HTTPException(422, f"unknown table {tbl}")
    if (tbl and fld not in TABLES[tbl].fields) or (not tbl and fld not in FIELD_DESCRIPTIONS):
        raise HTTPException(422, f"{fld} is not a field of {tbl or 'the table catalog'}")
    a = db.execute(select(CockpitAlias).where(CockpitAlias.project_id == project_id, CockpitAlias.alias == req.alias.upper(), CockpitAlias.table_name == tbl)).scalars().first()
    if a is None:
        a = CockpitAlias(project_id=project_id, alias=req.alias.upper(), field=fld, table_name=tbl, description=req.description[:200], evidence="entered by hand", created_by=p.username)
        db.add(a)
        db.flush()
    decide_alias(db, a, "CONFIRMED", p.username, fld)
    return alias_out(a)


@router.post("/projects/{project_id}/cockpit-aliases/{alias_id}/decide", tags=["runs"])
def cockpit_alias_decide(project_id: str, alias_id: str, req: CockpitAliasDecision, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..models import CockpitAlias
    from ..runtime.cockpit_templates import alias_out, decide_alias

    assert_project_access(db, p, project_id)
    a = db.get(CockpitAlias, alias_id)
    if a is None or a.project_id != project_id:
        raise HTTPException(404, "alias not found")
    try:
        decide_alias(db, a, req.status, p.username, req.field)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return alias_out(a)


@router.post("/projects/{project_id}/cockpit-aliases/propose", tags=["runs"])
def cockpit_alias_propose(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Re-read every registered template's Field List and record the aliases it proposes."""
    from ..runtime.cockpit_templates import alias_out, store_proposals, templates_for

    assert_project_access(db, p, project_id)
    out = []
    for _, t in sorted(templates_for(db, project_id).items()):
        out += [alias_out(a) for a in store_proposals(db, project_id, t, p.username)]
    return out


@router.delete("/projects/{project_id}/cockpit-aliases/{alias_id}", tags=["runs"], status_code=204)
def cockpit_alias_delete(project_id: str, alias_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..models import CockpitAlias

    assert_project_access(db, p, project_id)
    a = db.get(CockpitAlias, alias_id)
    if a is None or a.project_id != project_id:
        raise HTTPException(404, "alias not found")
    record_event(db, p.username, "COCKPIT_ALIAS_DELETED", "PROJECT", project_id, {"alias": a.alias, "field": a.field})
    db.delete(a)
    db.flush()
    return Response(status_code=204)


class MigrationObjectImport(BaseModel):
    entries: list[dict] = Field(..., description="[{name, id?, release?, object_types?, tables?, notes?}] as the target's object list shows them")
    release: str | None = Field(None, description="default release for entries without one; omit for any release")
    replace: bool = Field(False, description="drop the project's other entries first")
    source: str = Field("", description="where the list comes from (app export, documentation page)")


@router.get("/migration-objects/catalogue", tags=["runs"])
def migration_object_catalogue(release: str | None = None, p: Principal = Depends(require("project:read"))):
    """Documented migration objects, with the name valid in `release` (renames applied) and the unverified ID hints."""
    from ..catalog.migration_objects import (
        CATALOGUE_SOURCE,
        ON_PREMISE_RELEASES,
        catalogue,
        normalize_release,
    )

    return {"release": release, "normalized_release": normalize_release(release) if release else "", "releases": list(ON_PREMISE_RELEASES) + ["CLOUD"], "source": CATALOGUE_SOURCE, "objects": catalogue(release)}


@router.get("/migration-objects/lookup", tags=["runs"])
def migration_object_lookup(object_type: str, release: str, table: str | None = None, p: Principal = Depends(require("project:read"))):
    from ..catalog.migration_objects import lookup

    return lookup(object_type, release, table)


@router.get("/projects/{project_id}/migration-objects", tags=["runs"])
def project_migration_objects(project_id: str, release: str | None = None, object_type: str = "", db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """The project's registry (imported from the target) and the resolution per business object for the target's
    release (or `release`)."""
    from ..models import MigrationObjectEntry
    from ..runtime.migration_objects import entry_out, project_lookup

    assert_project_access(db, p, project_id)
    rows = db.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == project_id).order_by(MigrationObjectEntry.release, MigrationObjectEntry.name)).scalars().all()
    return {**project_lookup(db, project_id, release, object_type), "registry": [entry_out(e) for e in rows]}


@router.post("/projects/{project_id}/migration-objects/import", tags=["runs"], status_code=201)
def project_migration_objects_import(project_id: str, req: MigrationObjectImport, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..runtime.migration_objects import entry_out, import_entries

    assert_project_access(db, p, project_id)
    try:
        rows = import_entries(db, project_id, req.entries, p.username, req.release, req.replace, req.source)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return [entry_out(e) for e in rows]


@router.delete("/projects/{project_id}/migration-objects/{entry_id}", tags=["runs"], status_code=204)
def project_migration_object_delete(project_id: str, entry_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..models import MigrationObjectEntry

    assert_project_access(db, p, project_id)
    e = db.get(MigrationObjectEntry, entry_id)
    if e is None or e.project_id != project_id:
        raise HTTPException(404, "entry not found")
    record_event(db, p.username, "MIGRATION_OBJECT_DELETED", "PROJECT", project_id, {"name": e.name, "release": e.release})
    db.delete(e)
    db.flush()
    return Response(status_code=204)


class CockpitFeedbackIn(BaseModel):
    content: str = Field(..., description="the simulation message log as the app exports it: CSV/TSV, JSON, or SpreadsheetML XML")
    filename: str = "simulation-log.csv"
    replace: bool = Field(True, description="replace the feedback of the round the log answers (false: add to it)")
    round: int | None = Field(None, ge=1, description="the round (package) the log answers; default: the latest round not yet simulated")


@router.post("/runs/{run_id}/cockpit-feedback/import", tags=["runs"], status_code=201)
def run_cockpit_feedback_import(req: CockpitFeedbackIn, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Import the migration cockpit's upload simulation feedback: messages are matched to the exported instances,
    rejected instances are marked COCKPIT_ERROR with LOAD-stage exceptions, and a classified summary is recorded."""
    from ..runtime.cockpit_feedback import import_feedback

    try:
        return import_feedback(db, r.id, req.content, req.filename, p.username, req.replace, req.round)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/runs/{run_id}/cockpit-feedback", tags=["runs"])
def run_cockpit_feedback(severity: str | None = None, object_type: str | None = None, unmatched: bool | None = None, round: int | None = None, limit: int = Query(500, le=5000), r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..models import CockpitFeedback
    from ..runtime.cockpit_feedback import feedback_out

    stmt = select(CockpitFeedback).where(CockpitFeedback.run_id == r.id)
    if round is not None:
        stmt = stmt.where(CockpitFeedback.attempt_sequence == round)
    if severity:
        stmt = stmt.where(CockpitFeedback.severity == severity.upper()[:1])
    if object_type:
        stmt = stmt.where(CockpitFeedback.object_type == object_type)
    if unmatched is not None:
        stmt = stmt.where(CockpitFeedback.matched.is_(not unmatched))
    rows = db.execute(stmt.order_by(CockpitFeedback.severity, CockpitFeedback.object_type, CockpitFeedback.matched_key).limit(limit)).scalars().all()
    summary = (r.report or {}).get("cockpit_feedback") or {"imported": False}
    return {"summary": summary, "messages": [feedback_out(f) for f in rows]}


@router.delete("/runs/{run_id}/cockpit-feedback", tags=["runs"])
def run_cockpit_feedback_clear(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..runtime.cockpit_feedback import clear_feedback

    return {"cleared": clear_feedback(db, r.id, p.username)}


@router.get("/runs/{run_id}/cockpit-feedback/sample", tags=["runs"])
def run_cockpit_feedback_sample(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """An illustrative simulation log for this run's exported instances (not an SAP file), to exercise the import."""
    from ..runtime.cockpit_feedback import sample_feedback

    return Response(sample_feedback(db, r.id), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="simulation-log-{r.id}.sample.csv"'})


class CockpitRoundMark(BaseModel):
    status: str = Field(..., pattern="^(UPLOADED|MIGRATED)$")
    note: str = Field("", description="what was done in the app: project, transfer/upload id, who")


@router.get("/runs/{run_id}/cockpit-rounds", tags=["runs"])
def run_cockpit_rounds(r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """The package rounds of the run (full export and retry packages) with their upload/simulation/migration
    steps, the convergence line and the instances still rejected with their history."""
    from ..runtime.cockpit_attempts import burndown

    return burndown(db, r.id)


@router.post("/runs/{run_id}/cockpit-rounds/{sequence}/mark", tags=["runs"])
def run_cockpit_round_mark(sequence: int, req: CockpitRoundMark, r: MigrationRun = Depends(get_run), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Record by hand that a round's files were uploaded in the app, or that the app's migration step ran."""
    from ..runtime.cockpit_attempts import attempt_out, attempts, mark_attempt

    a = next((x for x in attempts(db, r.id) if x.sequence == sequence), None)
    if a is None:
        raise HTTPException(404, "round not found")
    try:
        return attempt_out(mark_attempt(db, a, req.status, p.username, req.note))
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


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


class RehearsalCreate(BaseModel):
    name: str = ""
    kind: str = Field("MOCK", pattern="^(MOCK|DRESS|FINAL|mock|dress|final)$", description="MOCK: mock cutover; DRESS: dress rehearsal; FINAL: the go-live checklist")


class RehearsalItemMark(BaseModel):
    status: str = Field(pattern="^(PENDING|PASS|FAIL|NOT_APPLICABLE)$")
    note: str = ""


class RehearsalTaskTiming(BaseModel):
    action: str = Field(pattern="^(start|finish)$")
    note: str = ""


class RehearsalNote(BaseModel):
    note: str = ""


class RehearsalLesson(BaseModel):
    text: str
    task: str = ""


class RehearsalVerdict(BaseModel):
    verdict: str = Field(pattern="^(GO|NO_GO)$")
    note: str = ""


def _get_rehearsal(rehearsal_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    from ..models import CutoverRehearsal

    r = db.get(CutoverRehearsal, rehearsal_id)
    if r is None:
        raise HTTPException(404, "rehearsal not found")
    assert_project_access(db, p, r.project_id)
    return r


def _rehearsal_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.get("/cutover/checklist-template", tags=["cutover"])
def cutover_checklist_template(p: Principal = Depends(current_principal)):
    """The checklist every rehearsal starts from: the automatic items the platform evaluates and the manual
    items recorded by hand."""
    from ..cutover.rehearsal import checklist_template

    return checklist_template()


@router.post("/manifests/{manifest_id}/cutover/rehearsals", tags=["cutover"], status_code=201)
def rehearsal_create(req: RehearsalCreate | None = None, m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Create a cutover rehearsal (mock cutover, dress rehearsal or the go-live checklist) for the manifest: the
    checklist with the automatic items evaluated now, and the runbook snapshot to time tasks against."""
    from ..cutover.rehearsal import create_rehearsal, rehearsal_out

    req = req or RehearsalCreate()
    return rehearsal_out(_rehearsal_call(create_rehearsal, db, m, req.name, req.kind, p.username))


@router.get("/manifests/{manifest_id}/cutover/rehearsals", tags=["cutover"])
def rehearsal_list(m: ScopeManifest = Depends(get_manifest), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..cutover.rehearsal import rehearsal_out, rehearsals

    return [rehearsal_out(r, full=False) for r in rehearsals(db, m.id)]


@router.get("/cutover/rehearsals/{rehearsal_id}", tags=["cutover"])
def rehearsal_get(r=Depends(_get_rehearsal), p: Principal = Depends(require("project:read"))):
    from ..cutover.rehearsal import rehearsal_out

    return rehearsal_out(r)


@router.get("/cutover/rehearsals/{rehearsal_id}/report", tags=["cutover"])
def rehearsal_report(r=Depends(_get_rehearsal), p: Principal = Depends(require("project:read"))):
    """The checklist as a Markdown report (for the evidence package / the cutover binder)."""
    from ..cutover.rehearsal import rehearsal_markdown, rehearsal_out

    return {"markdown": rehearsal_markdown(r), "rehearsal": rehearsal_out(r, full=False)}


@router.post("/cutover/rehearsals/{rehearsal_id}/refresh", tags=["cutover"])
def rehearsal_refresh(r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Re-evaluate the automatic items from the platform state."""
    from ..cutover.rehearsal import refresh_auto_items, rehearsal_out

    res = _rehearsal_call(refresh_auto_items, db, r, p.username)
    return {**rehearsal_out(r), "changed": res["changed"]}


@router.post("/cutover/rehearsals/{rehearsal_id}/start", tags=["cutover"])
def rehearsal_start(req: RehearsalNote | None = None, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..cutover.rehearsal import rehearsal_out, start_rehearsal

    return rehearsal_out(_rehearsal_call(start_rehearsal, db, r, p.username, (req or RehearsalNote()).note))


@router.post("/cutover/rehearsals/{rehearsal_id}/items/{item_id}", tags=["cutover"])
def rehearsal_mark_item(item_id: str, req: RehearsalItemMark, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Tick a manual item by hand (PASS, FAIL with a note, NOT_APPLICABLE, or back to PENDING); an automatic
    item can only be waived to NOT_APPLICABLE with a note, or un-waived."""
    from ..cutover.rehearsal import mark_item, rehearsal_out

    item = _rehearsal_call(mark_item, db, r, item_id, req.status, p.username, req.note)
    return {"item": item, "summary": r.summary, "rehearsal": rehearsal_out(r, full=False)}


@router.post("/cutover/rehearsals/{rehearsal_id}/tasks/{task_id}", tags=["cutover"])
def rehearsal_time_task(task_id: str, req: RehearsalTaskTiming, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    """Record by hand that a runbook task started or finished; the measured minutes feed the next runbook."""
    from ..cutover.rehearsal import time_task

    return {"task": task_id, **_rehearsal_call(time_task, db, r, task_id, req.action, p.username, req.note), "summary": r.summary}


@router.post("/cutover/rehearsals/{rehearsal_id}/lessons", tags=["cutover"], status_code=201)
def rehearsal_add_lesson(req: RehearsalLesson, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..cutover.rehearsal import add_lesson

    return _rehearsal_call(add_lesson, db, r, req.text, p.username, req.task)


@router.post("/cutover/rehearsals/{rehearsal_id}/complete", tags=["cutover"])
def rehearsal_complete(req: RehearsalVerdict, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("approve:run"))):
    """The approver's verdict: GO (refused while a blocking item is not PASS / NOT_APPLICABLE) or NO_GO."""
    from ..cutover.rehearsal import complete_rehearsal, rehearsal_out

    return rehearsal_out(_rehearsal_call(complete_rehearsal, db, r, req.verdict, p.username, req.note))


@router.post("/cutover/rehearsals/{rehearsal_id}/abort", tags=["cutover"])
def rehearsal_abort(req: RehearsalNote | None = None, r=Depends(_get_rehearsal), db: Session = Depends(get_db), p: Principal = Depends(require("run:start"))):
    from ..cutover.rehearsal import abort_rehearsal, rehearsal_out

    return rehearsal_out(_rehearsal_call(abort_rehearsal, db, r, p.username, (req or RehearsalNote()).note))


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
    {"area": "Target load", "status": "SIMULATED", "note": "Initial load and delta cycles go through the released S/4HANA APIs (business partner, product, sales/purchase order, delivery, journal entry with target numbering) and the migration cockpit for histories and cockpit objects, on the simulated gateway or an HTTPS target; verified on the simulated gateway only (ADR-0015). load_mode=direct keeps the simulated direct loader"},
    {"area": "Migration cockpit staging-file export", "status": "IMPLEMENTED", "note": "CSV per staging table and SpreadsheetML workbook per migration object for the rows the initial load routes to the cockpit, with manifest, checksums and zip; generic workbooks are not the target's templates (migration object names are hints to verify)"},
    {"area": "Template-driven cockpit export", "status": "IMPLEMENTED", "note": "Registered migration object templates (the app's XML workbooks) are parsed (Field List incl. hidden SAP Structure/SAP Field columns, hidden technical rows, merged key cell), mapped automatically (same names, BAPI-style aliases, parent/related keys, recorded overrides) with a coverage report, and filled with typed, line-oriented cells; verified against the layout SAP documents and SAP's own XML file splitter on filled files, not against a template downloaded from a release (check endpoint and CLI report deviations); alias catalogue of BAPI-style template names extended from the public BAPI structures, project aliases learned from a template's Field List by DDIC description match and confirmed by an architect; migration object lookup per target release (documented names with renames and availability, unverified ID hints) with a project registry imported from the target's object list; upload simulation feedback import (the app's message log matched to the exported instances, COCKPIT_ERROR statuses and exceptions, classified summary, retry package of rejected instances); re-upload tracking: every package is a round (exported, uploaded, simulated, migrated, superseded) with per-instance outcomes, released instances, and a burn-down of the still-rejected ones across rounds"},
    {"area": "Reconciliation (technical/functional/financial)", "status": "IMPLEMENTED", "note": "Source read through the RFC add-on (company-code pushdown, Z_SDTF_AGGREGATE counts and totals prove the read complete), target read back through the released APIs (entities by key, filtered collections, journal entry items); tables without a read path are reported as not verified instead of failing; re-run on a completed run with POST /runs/{id}/reconcile; verified on the simulated add-on and gateway only"},
    {"area": "Audit trail & evidence packages", "status": "IMPLEMENTED", "note": "Hash-chained events, evidence index"},
    {"area": "AI agents", "status": "IMPLEMENTED", "note": "12 bounded heuristic agents; LLM reasoner planned"},
    {"area": "Multi-source merger / consolidation", "status": "IMPLEMENTED", "note": "Merge groups, cross-system key collision planning, master-data dedup, group-level financial reconciliation (simulated runtime)"},
    {"area": "Delta capture / near-zero downtime", "status": "SIMULATED", "note": "CDC through the SAP add-on contract (Z_SDTF_CDC_POLL) over RFC, ordered idempotent replay, freeze, final delta + full reconciliation; verified on the simulated add-on only, no downtime figure claimed (ADR-0014)"},
    {"area": "Cutover command center", "status": "PARTIAL", "note": "Runbook generation, critical path, forecast; cutover rehearsal checklist (mock cutover / dress rehearsal / go-live): automatic readiness items evaluated from the platform state, manual items ticked by hand with audit, runbook task timings measured by hand and fed back into the forecast, lessons, approver GO / NO_GO refused while a blocking item is open; live execution tracking of the production cutover, incident escalation and resource assignment planned"},
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

"""Migration run orchestrator for the vertical slice (SIMULATED mode).

Preconditions enforced: approved + integrity-checked manifest, approved ruleset, systems belong to the project,
and SIMULATED mode (the only mode this build supports). Stages are persisted with checkpoints so a run can be
resumed after a failure without re-extracting completed partitions.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .. import config
from ..audit.service import record_event, write_evidence_package
from ..catalog.store import RecordStore
from ..models import (
    MigrationRun,
    ReconciliationResult,
    RuleSet,
    RunStage,
    SapSystem,
    ScopeManifest,
    TransformationException,
)
from ..reconciliation.service import reconcile_run
from ..rules.engine import parse_ruleset
from ..scope.service import verify_manifest_integrity
from ..staging import get_backend
from .extraction import SyntheticStoreExtractor, run_extraction
from .load import SimulatedTargetLoader
from .transform import run_transformation

STAGES = ["PRECHECK", "EXTRACT", "TRANSFORM", "LOAD", "RECONCILE", "REPORT"]
SUPPORTED_MODES = {"SIMULATED"}


class RunPrecondition(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def start_run(session: Session, project_id: str, manifest_id: str, ruleset_id: str, actor: str, mode: str = "SIMULATED", workers: int | None = None, merge_group: str | None = None, execution: str = "INLINE", staging_backend: str | None = None, pipelined: bool = True) -> MigrationRun:
    if mode not in SUPPORTED_MODES:
        raise RunPrecondition(f"mode {mode} is not supported by this build; only SIMULATED runs exist (no production SAP connectivity)")
    m = session.get(ScopeManifest, manifest_id)
    rs = session.get(RuleSet, ruleset_id)
    if m is None or m.project_id != project_id:
        raise RunPrecondition("manifest not found in project")
    if rs is None or rs.project_id != project_id:
        raise RunPrecondition("ruleset not found in project")
    if m.status != "APPROVED":
        raise RunPrecondition(f"manifest {m.name} v{m.version} is {m.status}; only APPROVED manifests can be executed")
    if not verify_manifest_integrity(m):
        raise RunPrecondition("manifest content hash mismatch: manifest was tampered with")
    if rs.status != "APPROVED":
        raise RunPrecondition(f"ruleset {rs.name} v{rs.version} is {rs.status}; only APPROVED rulesets can be executed")
    src = session.get(SapSystem, m.definition["source_system_id"])
    tgt = session.get(SapSystem, m.definition["target_system_id"])
    if src is None or tgt is None or src.project_id != project_id or tgt.project_id != project_id:
        raise RunPrecondition("source/target systems not found in project")
    if execution not in ("INLINE", "DISTRIBUTED"):
        raise RunPrecondition(f"unknown execution mode {execution}")
    backend_name = staging_backend or config.settings.staging_backend
    run = MigrationRun(project_id=project_id, manifest_id=m.id, ruleset_id=rs.id, source_system_id=src.id, target_system_id=tgt.id, mode=mode, status="RUNNING", started_by=actor, started_at=_now(), metrics={"workers": workers or config.settings.extraction_workers, "execution": execution, "staging_backend": backend_name, **({"pipelined": pipelined} if execution == "DISTRIBUTED" else {}), **({"merge_group": merge_group} if merge_group else {})})
    session.add(run)
    session.flush()
    for i, name in enumerate(STAGES):
        session.add(RunStage(run_id=run.id, sequence=i, name=name))
    session.flush()
    record_event(session, actor, "RUN_STARTED", "RUN", run.id, {"manifest": m.id, "ruleset": rs.id, "mode": mode, "execution": execution, "staging": backend_name})
    if execution == "DISTRIBUTED":
        from .worker import enqueue_extraction_jobs

        _stage(run, "PRECHECK").status = "DONE"
        _stage(run, "PRECHECK").metrics = {"manifest_hash": m.content_hash, "ruleset_hash": rs.content_hash, "objects_in_scope": m.impact.get("objects_total", 0)}
        st = _stage(run, "EXTRACT")
        st.status, st.started_at = "RUNNING", _now()
        st.metrics = {"jobs": enqueue_extraction_jobs(session, run), "execution": "DISTRIBUTED", "pipelined": pipelined}
        session.flush()
        return run
    return execute_run(session, run, actor)


def resume_run(session: Session, run_id: str, actor: str) -> MigrationRun:
    run = session.get(MigrationRun, run_id)
    if run is None:
        raise RunPrecondition("run not found")
    if run.status not in ("FAILED", "RUNNING", "ADVANCING"):
        raise RunPrecondition(f"run is {run.status}; only FAILED runs can be resumed")
    record_event(session, actor, "RUN_RESUMED", "RUN", run.id, {})
    run.status = "RUNNING"
    if run.metrics.get("execution") == "DISTRIBUTED":
        from .worker import JOB_STAGES, repair_pipeline, requeue_jobs

        current = next((s for s in JOB_STAGES if _stage(run, s).status != "DONE"), None)
        if current is not None:  # a job stage is still open: re-queue failed / orphaned jobs and missing successors
            st = _stage(run, current)
            st.status = "RUNNING"
            st.metrics = {**(st.metrics or {}), "requeued": requeue_jobs(session, run.id, statuses=("FAILED", "CLAIMED"))}
            session.flush()
            st.metrics = {**st.metrics, "repaired": repair_pipeline(session, run.id)}
            session.flush()
            return run
    return execute_run(session, run, actor)


def _stage(run: MigrationRun, name: str) -> RunStage:
    return next(s for s in run.stages if s.name == name)


def execute_run(session: Session, run: MigrationRun, actor: str) -> MigrationRun:
    m = session.get(ScopeManifest, run.manifest_id)
    rs_row = session.get(RuleSet, run.ruleset_id)
    src = session.get(SapSystem, run.source_system_id)
    tgt = session.get(SapSystem, run.target_system_id)
    rs = parse_ruleset(rs_row.source_yaml)
    cls = m.selection.get("classification", {})
    scope_ccs = set(m.definition["company_codes"])
    source_store = None
    backend = get_backend(run.metrics.get("staging_backend"), session=session)
    try:
        for name in STAGES:
            st = _stage(run, name)
            if st.status == "DONE":
                continue
            st.status, st.started_at = "RUNNING", _now()
            session.flush()
            t0 = time.monotonic()
            if name == "PRECHECK":
                st.metrics = {"manifest_hash": m.content_hash, "ruleset_hash": rs_row.content_hash, "source": f"{src.sid}/{src.client}", "target": f"{tgt.sid}/{tgt.client}", "objects_in_scope": m.impact.get("objects_total", 0)}
            elif name == "EXTRACT":
                source_store = source_store or RecordStore.load(session, src.id)
                ex = SyntheticStoreExtractor(source_store, cls, scope_ccs)
                st.metrics = run_extraction(session, run.id, ex, st.checkpoint, workers=run.metrics.get("workers", config.settings.extraction_workers), backend=backend)
                run.snapshot_id = st.metrics["snapshot_id"]
            elif name == "TRANSFORM":
                st.metrics = run_transformation(session, run.id, rs, backend=backend)
            elif name == "LOAD":
                st.metrics = SimulatedTargetLoader(session, tgt, run.id, backend=backend).load()
            elif name == "RECONCILE":
                source_store = source_store or RecordStore.load(session, src.id)
                session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
                target_store = RecordStore.load(session, tgt.id)
                st.metrics = reconcile_run(session, run, m, source_store, target_store, financial=not run.metrics.get("merge_group"), backend=backend)
            elif name == "REPORT":
                # the report stage is marked complete before rendering so the report reflects final stage states
                st.status, st.finished_at = "DONE", _now()
                st.duration_ms = round((time.monotonic() - t0) * 1000, 1)
                run.report = build_report(session, run, m, rs_row)
                st.metrics = {"evidence_files": len(run.report.get("evidence", {}).get("files", {}))}
            st.status, st.finished_at = "DONE", _now()
            st.duration_ms = max(st.duration_ms, round((time.monotonic() - t0) * 1000, 1))
            session.flush()
        run.status = "COMPLETED"
        run.finished_at = _now()
        record_event(session, actor, "RUN_COMPLETED", "RUN", run.id, {"reconciliation": run.report.get("reconciliation", {}).get("overall")})
    except Exception as e:  # noqa: BLE001 - we persist the failure and re-raise
        for s in run.stages:
            if s.status == "RUNNING":
                s.status = "FAILED"
                s.metrics = {**(s.metrics or {}), "error": str(e)}
        run.status = "FAILED"
        run.finished_at = _now()
        session.flush()
        record_event(session, actor, "RUN_FAILED", "RUN", run.id, {"error": str(e)})
        raise
    session.flush()
    return run


def build_report(session: Session, run: MigrationRun, m: ScopeManifest, rs_row: RuleSet) -> dict:
    stages = [{"name": s.name, "status": s.status, "duration_ms": s.duration_ms, "metrics": s.metrics} for s in run.stages]
    recon = session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).all()
    exceptions = session.query(TransformationException).filter(TransformationException.run_id == run.id).all()
    from ..reconciliation.service import summarize

    recon_summary = summarize(recon)
    failures = [{"layer": r.layer, "check": r.check_name, "subject": r.subject, "status": r.status, "source": r.source_value, "target": r.target_value, "variance": r.variance, "explanation": r.explanation} for r in recon if r.status != "PASS"]
    report = {
        "run_id": run.id,
        "mode": run.mode,
        "disclaimer": "SIMULATED run against synthetic data. No SAP system was read or written. Results demonstrate the engine, not production migration capability.",
        "manifest": {"id": m.id, "name": m.name, "version": m.version, "hash": m.content_hash, "approved_by": m.approved_by, "company_codes": m.definition["company_codes"], "impact": m.impact},
        "ruleset": {"id": rs_row.id, "name": rs_row.name, "version": rs_row.version, "hash": rs_row.content_hash, "approved_by": rs_row.approved_by},
        "snapshot_id": run.snapshot_id,
        "stages": stages,
        "reconciliation": {**recon_summary, "failures": failures[:200]},
        "exceptions": {"count": len(exceptions), "by_stage": _count(exceptions, "stage"), "samples": [{"stage": e.stage, "table": e.table_name, "key": e.record_key, "rule": e.rule_id, "message": e.message} for e in exceptions[:50]]},
        "capability_status": {"extraction": "SIMULATED", "load": "SIMULATED", "transformation": "IMPLEMENTED", "reconciliation": "IMPLEMENTED", "delta_sync": "PLANNED", "cutover": "PLANNED"},
    }
    md = render_markdown(report)
    evidence = write_evidence_package(run.id, {"report.json": report, "report.md": md, "manifest.json": {"definition": m.definition, "impact": m.impact, "hash": m.content_hash}, "ruleset.yaml": rs_row.source_yaml, "reconciliation.json": [{"layer": r.layer, "check": r.check_name, "subject": r.subject, "status": r.status, "source": r.source_value, "target": r.target_value, "variance": r.variance, "explanation": r.explanation} for r in recon]})
    report["evidence"] = evidence
    report["markdown"] = md
    return report


def _count(items, attr):
    out: dict[str, int] = {}
    for i in items:
        out[getattr(i, attr)] = out.get(getattr(i, attr), 0) + 1
    return out


def render_markdown(report: dict) -> str:
    lines = [f"# Migration run {report['run_id']} ({report['mode']})", "", f"> {report['disclaimer']}", ""]
    m = report["manifest"]
    lines += [f"**Manifest:** {m['name']} v{m['version']} (hash {m['hash'][:12]}…, approved by {m['approved_by']})", f"**Company codes:** {', '.join(m['company_codes'])}", f"**Ruleset:** {report['ruleset']['name']} v{report['ruleset']['version']} (approved by {report['ruleset']['approved_by']})", f"**Snapshot:** {report['snapshot_id']}", "", "## Stages", "", "| Stage | Status | Duration (ms) | Key metrics |", "|---|---|---|---|"]
    for s in report["stages"]:
        keys = {k: v for k, v in (s["metrics"] or {}).items() if not isinstance(v, (dict, list))}
        lines.append(f"| {s['name']} | {s['status']} | {s['duration_ms']} | {', '.join(f'{k}={v}' for k, v in list(keys.items())[:6])} |")
    r = report["reconciliation"]
    lines += ["", "## Reconciliation", "", f"Overall: **{r['overall']}** over {r['checks']} checks", ""]
    for layer, counts in r["by_layer"].items():
        lines.append(f"- {layer}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if r["failures"]:
        lines += ["", "### Non-passing checks", "", "| Layer | Check | Subject | Status | Source | Target | Variance | Explanation |", "|---|---|---|---|---|---|---|---|"]
        for f in r["failures"][:60]:
            lines.append(f"| {f['layer']} | {f['check']} | {f['subject']} | {f['status']} | {f['source']} | {f['target']} | {f['variance']} | {f['explanation']} |")
    e = report["exceptions"]
    lines += ["", "## Exceptions", "", f"{e['count']} exception(s): {e['by_stage']}", ""]
    lines += ["## Capability status", ""] + [f"- {k}: {v}" for k, v in report["capability_status"].items()]
    return "\n".join(lines) + "\n"

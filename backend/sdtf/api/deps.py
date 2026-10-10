from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MigrationRun, RuleSet, SapSystem, ScopeManifest
from ..rules.editor import decisions_summary
from ..security.auth import Principal, assert_project_access, current_principal


def get_system(system_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> SapSystem:
    s = db.get(SapSystem, system_id)
    if s is None:
        raise HTTPException(404, "system not found")
    assert_project_access(db, p, s.project_id)
    return s


def get_manifest(manifest_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> ScopeManifest:
    m = db.get(ScopeManifest, manifest_id)
    if m is None:
        raise HTTPException(404, "manifest not found")
    assert_project_access(db, p, m.project_id)
    return m


def get_ruleset(ruleset_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> RuleSet:
    r = db.get(RuleSet, ruleset_id)
    if r is None:
        raise HTTPException(404, "ruleset not found")
    assert_project_access(db, p, r.project_id)
    return r


def get_run(run_id: str, db: Session = Depends(get_db), p: Principal = Depends(current_principal)) -> MigrationRun:
    r = db.get(MigrationRun, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    assert_project_access(db, p, r.project_id)
    return r


def manifest_out(m: ScopeManifest, full: bool = False) -> dict:
    d = {"id": m.id, "project_id": m.project_id, "name": m.name, "version": m.version, "status": m.status, "content_hash": m.content_hash, "created_by": m.created_by, "approved_by": m.approved_by, "approved_at": m.approved_at, "created_at": m.created_at, "definition": m.definition, "impact": m.impact}
    if full:
        d["selection"] = m.selection
    return d


def ruleset_out(r: RuleSet, full: bool = False) -> dict:
    d = {"id": r.id, "project_id": r.project_id, "name": r.name, "version": r.version, "status": r.status, "content_hash": r.content_hash, "created_by": r.created_by, "approved_by": r.approved_by, "created_at": r.created_at, "validation": r.validation, "rule_count": len(r.compiled.get("rules", [])), "decisions": decisions_summary(r)}
    if full:
        d["source_yaml"] = r.source_yaml
        d["compiled"] = r.compiled
    return d


def run_out(r: MigrationRun, full: bool = False) -> dict:
    d = {"id": r.id, "project_id": r.project_id, "manifest_id": r.manifest_id, "ruleset_id": r.ruleset_id, "source_system_id": r.source_system_id, "target_system_id": r.target_system_id, "mode": r.mode, "status": r.status, "started_by": r.started_by, "started_at": r.started_at, "finished_at": r.finished_at, "snapshot_id": r.snapshot_id, "metrics": r.metrics, "stages": [{"name": s.name, "sequence": s.sequence, "status": s.status, "started_at": s.started_at, "finished_at": s.finished_at, "duration_ms": s.duration_ms, "metrics": s.metrics, "checkpoint": s.checkpoint} for s in r.stages], "reconciliation": (r.report or {}).get("reconciliation", {}).get("overall")}
    if full:
        d["report"] = r.report
    return d

"""Reconciliation and rule engine must detect injected defects, never mask them."""
from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.demo import approve_ruleset
from sdtf.models import MigrationRun, ReconciliationResult, RuleSet, SapRecord, SapSystem, ScopeManifest
from sdtf.reconciliation.service import reconcile_run
from sdtf.runtime.pipeline import start_run


def test_tampered_target_record_is_detected(session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    m = session.get(ScopeManifest, run.manifest_id)
    rec = session.execute(select(SapRecord).where(SapRecord.system_id == run.target_system_id, SapRecord.table_name == "BSEG")).scalars().first()
    original = dict(rec.payload)
    rec.payload = {**original, "DMBTR": float(original["DMBTR"]) + 1000.0}
    session.flush()
    session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
    summary = reconcile_run(session, run, m, RecordStore.load(session, run.source_system_id), RecordStore.load(session, run.target_system_id))
    assert summary["overall"] == "FAIL"
    checks = {(r.check_name, r.subject): r for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.status == "FAIL")).scalars()}
    assert ("checksum", "BSEG") in checks and ("field_comparison", "BSEG") in checks
    assert any(k[0] == "trial_balance" for k in checks), "a one-sided amount change must unbalance the trial balance"
    assert any(k[0] == "gl_balance" and "loaded content differs" in v.explanation for k, v in checks.items())
    rec.payload = original
    session.flush()
    session.query(ReconciliationResult).filter(ReconciliationResult.run_id == run.id).delete()
    assert reconcile_run(session, run, m, RecordStore.load(session, run.source_system_id), RecordStore.load(session, run.target_system_id))["overall"] == "PASS"


def test_rejecting_rule_produces_exceptions_and_explained_variance(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    base = session.get(RuleSet, slice_result["ruleset_id"])
    import yaml

    doc = yaml.safe_load(base.source_yaml)
    doc["ruleset"] = "reject-test"
    doc["rules"].append({"id": "reject-payroll", "type": "reject", "tables": ["BSEG"], "when": {"field": "HKONT", "equals": "420000"}, "message": "payroll lines are not migrated in this test"})
    yaml_src = yaml.safe_dump(doc, sort_keys=False)
    from sdtf.rules.engine import parse_ruleset, validate_ruleset

    rs = parse_ruleset(yaml_src)
    v = validate_ruleset(rs)
    assert v["ok"], v
    row = RuleSet(project_id=m.project_id, name="reject-test", version=1, content_hash=rs.content_hash, source_yaml=yaml_src, compiled={"rules": rs.rules}, validation=v, created_by="architect")
    session.add(row)
    session.flush()
    approve_ruleset(session, row, "approver")
    # fresh target so the rejected lines are really absent
    from sdtf.demo import create_target_shell
    from sdtf.models import Project

    tgt = create_target_shell(session, session.get(Project, m.project_id), "TREJ")
    m2 = ScopeManifest(project_id=m.project_id, name="reject-test", version=1, content_hash="", definition={**m.definition, "target_system_id": tgt.id}, impact=m.impact, selection=m.selection, status="DRAFT", created_by="architect")
    from sdtf.scope.service import approve_manifest, canonical_hash

    m2.content_hash = canonical_hash({"definition": m2.definition, "classification": m2.selection["classification"], "version": 1})
    session.add(m2)
    session.flush()
    approve_manifest(session, m2, "approver")
    run = start_run(session, m.project_id, m2.id, row.id, "operator")
    assert run.status == "COMPLETED"
    rep = run.report
    assert rep["exceptions"]["count"] > 0 and rep["exceptions"]["by_stage"] == {"TRANSFORM": rep["exceptions"]["count"]}
    assert all(e["rule"] == "reject-payroll" for e in rep["exceptions"]["samples"])
    # rejected lines unbalance the documents in the target: trial balance must FAIL, and the payroll account
    # variance must be explained by the rejected amount
    checks = {(r.check_name, r.subject): r for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id)).scalars()}
    assert checks[("trial_balance", "SP01")].status == "FAIL"
    payroll = checks[("gl_balance", "5000->SP01/420000")]
    assert payroll.status == "WARN" and "rejected by transformation rules" in payroll.explanation and "unexplained 0.0" in payroll.explanation
    assert session.get(SapSystem, tgt.id) is not None

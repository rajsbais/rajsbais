import json
import os

import pytest
from sqlalchemy import select

from sdtf.audit.service import verify_chain
from sdtf.models import MigrationRun, ReconciliationResult, SapRecord, ScopeManifest, StagedRecord
from sdtf.runtime import pipeline
from sdtf.runtime.pipeline import RunPrecondition, resume_run, start_run


def test_vertical_slice_completes_and_reconciles(slice_result):
    assert slice_result["run_status"] == "COMPLETED"
    rep = slice_result["report"]
    assert rep["reconciliation"]["overall"] == "PASS", rep["reconciliation"]["failures"][:10]
    assert set(rep["reconciliation"]["by_layer"]) == {"TECHNICAL", "FUNCTIONAL", "FINANCIAL"}
    assert rep["exceptions"]["count"] == 0
    assert [s["name"] for s in rep["stages"]] == ["PRECHECK", "EXTRACT", "TRANSFORM", "LOAD", "RECONCILE", "REPORT"]
    assert all(s["status"] == "DONE" for s in rep["stages"])
    assert "SIMULATED" in rep["disclaimer"]


def test_evidence_package_written_with_hashes(slice_result):
    ev = slice_result["report"]["evidence"]
    assert os.path.isdir(ev["dir"])
    for name in ("report.json", "report.md", "manifest.json", "ruleset.yaml", "reconciliation.json"):
        assert name in ev["files"] and os.path.exists(ev["files"][name]["path"])
        assert len(ev["files"][name]["sha256"]) == 64
    idx = json.load(open(os.path.join(ev["dir"], "evidence_index.json")))
    assert set(idx) == set(ev["files"])


def test_target_contains_only_spinco_company_code(session, slice_result):
    ccs = {r[0] for r in session.execute(select(SapRecord.bukrs).where(SapRecord.system_id == slice_result["target_id"], SapRecord.table_name == "BKPF"))}
    assert ccs == {"SP01"}
    # business partner conversion applied
    kna1 = session.execute(select(SapRecord.record_key).where(SapRecord.system_id == slice_result["target_id"], SapRecord.table_name == "KNA1")).scalars().all()
    assert kna1 and all(k.startswith("BP") for k in kna1)


def test_financial_reconciliation_checks_present(session, slice_result):
    checks = {r[0] for r in session.execute(select(ReconciliationResult.check_name).where(ReconciliationResult.run_id == slice_result["run_id"]))}
    assert {"record_count", "key_uniqueness", "checksum", "field_comparison", "referential_integrity", "document_chain", "business_partner_reference", "material_reference", "open_document_validity", "organizational_assignment", "trial_balance", "gl_balance", "ar_open_items", "ap_open_items", "asset_balances", "inventory_valuation", "intercompany_balance", "currency_totals", "fiscal_period_control"} <= checks


def test_audit_chain_is_intact(session, slice_result):
    v = verify_chain(session)
    assert v["ok"] and v["events"] > 5


def test_rerun_is_idempotent(session, slice_result):
    run = start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    load = next(s for s in run.stages if s.name == "LOAD").metrics
    assert load["loaded"] == 0 and load["skipped_duplicate"] > 0 and load["conflicts"] == 0
    assert run.report["reconciliation"]["overall"] == "PASS"


def test_unapproved_manifest_is_refused(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    draft = ScopeManifest(project_id=m.project_id, name="draft", version=1, content_hash=m.content_hash, definition=m.definition, impact=m.impact, selection=m.selection, status="DRAFT", created_by="architect")
    session.add(draft)
    session.flush()
    with pytest.raises(RunPrecondition, match="APPROVED"):
        start_run(session, m.project_id, draft.id, slice_result["ruleset_id"], "operator")
    with pytest.raises(RunPrecondition, match="not supported"):
        start_run(session, m.project_id, m.id, slice_result["ruleset_id"], "operator", mode="PRODUCTION")
    session.rollback()


def test_tampered_manifest_is_refused(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    original = dict(m.definition)
    m.definition = {**m.definition, "company_codes": ["5000", "1000"]}
    session.flush()
    with pytest.raises(RunPrecondition, match="tampered"):
        start_run(session, m.project_id, m.id, slice_result["ruleset_id"], "operator")
    m.definition = original
    session.flush()


def test_failed_run_can_be_resumed_without_re_extracting(session, slice_result, monkeypatch):
    calls = {"n": 0}
    real = pipeline.run_transformation

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected failure")
        return real(*a, **k)

    monkeypatch.setattr(pipeline, "run_transformation", flaky)
    with pytest.raises(RuntimeError):
        start_run(session, slice_result["project_id"], slice_result["manifest_id"], slice_result["ruleset_id"], "operator")
    run = session.execute(select(MigrationRun).where(MigrationRun.status == "FAILED")).scalars().first()
    assert run is not None
    assert next(s for s in run.stages if s.name == "EXTRACT").status == "DONE"
    assert next(s for s in run.stages if s.name == "TRANSFORM").status == "FAILED"
    staged_before = session.execute(select(StagedRecord).where(StagedRecord.run_id == run.id)).scalars().all()
    resumed = resume_run(session, run.id, "operator")
    assert resumed.status == "COMPLETED"
    assert next(s for s in resumed.stages if s.name == "EXTRACT").metrics["records"] == len(staged_before)
    assert resumed.report["reconciliation"]["overall"] == "PASS"

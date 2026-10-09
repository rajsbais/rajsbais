import copy
import json
import zipfile
import io

import pytest

from rfactory.sap.adapter import ProductionWriteBlocked, SapSystem, TransientError
from rfactory.sap.synthetic import SimulatedSap, build_source_dataset
from rfactory.selective.conflicts import Action
from rfactory.security.auth import Forbidden, DEMO_USERS as U
from rfactory.service import Conflict
from .conftest import norm, ADMIN, ALICE, BOB, CAROL, approved_project, make_project, ready_project


def test_mvp_vertical_slice_end_to_end(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    assert run.status == "COMPLETED", run.error
    rec = run.reconciliation
    assert rec["release"] == "RELEASED", [c for c in rec["checks"] if c["status"] != "pass"]
    assert p.status == "COMPLETED"
    assert {c["category"] for c in rec["checks"]} == {"technical", "business", "security"}
    tgt = svc.adapters[svc.tgt_id]
    # loaded objects are really in the target; excluded ones are not
    for iid in run.loaded:
        inst = p.plan.instances[iid]
        h = inst.rows[svc.registry.types[inst.type].header][0]
        assert tgt.get(svc.registry.types[inst.type].header, tuple(h[k] for k in svc.registry.types[inst.type].header_keys)) is not None
    for iid in run.quarantined:
        inst = p.plan.instances[iid]
        hdr = svc.registry.types[inst.type].header
        if hdr in ("VBAK", "LIKP", "VBRK"):
            assert tgt.get(hdr, (inst.key,)) is None or iid in p.report.equivalent_in_target
    # tester-owned colliding orders were left alone
    for f in p.report.findings:
        if f.details.get("owner"):
            assert tgt.get("VBAK", (f.instance.split(":")[1],))["ERNAM"] == "QA_ALICE"


def test_masked_everywhere_no_source_pii_in_target(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    src, tgt = svc.adapters[svc.src_id], svc.adapters[svc.tgt_id]
    originals = set()
    for t, f in [("KNA1", "NAME1"), ("KNA1", "TELF1"), ("KNA1", "STCD1"), ("KNA1", "ZZ_CONTACT_EMAIL"), ("ADRC", "SMTP_ADDR"),
                 ("KNBK", "IBAN"), ("KNBK", "BANKN"), ("ADRC", "STREET")]:
        originals |= {r[f] for r in src.data[t]}
    blob = json.dumps(tgt.data, default=str)
    loaded_customers = [i for i in run.loaded if i.startswith("CUSTOMER")]
    assert loaded_customers
    for v in originals:
        assert v not in blob, f"source PII value {v!r} leaked into target"
    # relationships survive masking: keys identical to source
    for iid in loaded_customers:
        assert tgt.get("KNA1", (iid.split(":")[1],)) is not None


def test_number_ranges_protected_after_load(svc):
    p = approved_project(svc)
    svc.execute(ALICE, p.id)
    tgt = svc.adapters[svc.tgt_id]
    for obj, level in p.report.number_range_adjustments.items():
        assert tgt.number_level(obj) >= level


def test_execution_is_idempotent(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    snap = copy.deepcopy(svc.adapters[svc.tgt_id].data)
    run.checkpoint = 0; run.loaded.clear()
    svc.executor.execute(run, p.plan, p.report, svc.engines[p.id], svc.adapters[svc.tgt_id], "alice.basis")
    assert norm(svc.adapters[svc.tgt_id].data) == norm(snap)


def test_staging_contains_only_masked_data(svc, tmp_path):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    staged = "".join(f.read_text() for f in (tmp_path / "staging" / run.id).glob("*.jsonl"))
    src = svc.adapters[svc.src_id]
    for r in src.data["KNA1"]:
        assert r["NAME1"] not in staged and r["STCD1"] not in staged
    assert run.staging_hashes


def test_approval_gates(svc):
    p = ready_project(svc)
    with pytest.raises(Conflict):
        svc.execute(ALICE, p.id)                    # not submitted/approved
    svc.submit(ALICE, p.id)
    with pytest.raises(Conflict):
        svc.execute(ALICE, p.id)                    # submitted but not approved
    svc.approve(CAROL, p.id)
    p.manifests.append(p.manifest.model_copy(update={"include_downstream": []}))  # tamper after approval
    with pytest.raises(Conflict, match="does not match"):
        svc.execute(ALICE, p.id)


def test_changing_manifest_invalidates_plan_and_approval(svc):
    p = approved_project(svc)
    m = p.manifest
    svc.set_manifest(ALICE, p.id, m.scope, [], m.masking_policy_id, m.conflict_policy)
    assert p.status == "DRAFT" and p.approval is None and p.plan is None
    with pytest.raises(Conflict):
        svc.submit(ALICE, p.id)


def test_cannot_submit_with_blocking_conflicts_or_uncovered_pii(svc):
    p = make_project(svc)  # default policy: duplicates fail
    svc.build_plan(ALICE, p.id); svc.analyze_conflicts(ALICE, p.id)
    with pytest.raises(Conflict, match="blocking target conflict"):
        svc.submit(ALICE, p.id)
    p2 = make_project(svc, {"DUPLICATE_DIFFERENT": "SKIP"})
    svc.build_plan(ALICE, p2.id); svc.analyze_conflicts(ALICE, p2.id)
    with pytest.raises(Conflict, match="sensitive field"):
        svc.submit(ALICE, p2.id)  # ZZ_CONTACT_EMAIL discovered by pattern, no rule yet


def test_separation_of_duties_and_agent_restrictions(svc):
    p = ready_project(svc)
    svc.submit(ALICE, p.id)
    with pytest.raises(Forbidden):
        svc.approve(ALICE, p.id)                    # no approver permission
    with pytest.raises(Forbidden):
        svc.approve(U["refresh.copilot"], p.id)     # AI agent holds the role but is stripped of the permission
    p_admin = svc.create_project(ADMIN, "admin-made", svc.src_id, svc.tgt_id)
    m = make_project(svc).manifest
    svc.set_manifest(ADMIN, p_admin.id, m.scope, [], "gdpr-standard", {"DUPLICATE_DIFFERENT": "SKIP"})
    svc.build_plan(ADMIN, p_admin.id)
    svc.add_masking_rules(ADMIN, p_admin.id, [{"table": "KNA1", "field": "ZZ_CONTACT_EMAIL", "strategy": "EMAIL"}])
    svc.analyze_conflicts(ADMIN, p_admin.id); svc.submit(ADMIN, p_admin.id)
    with pytest.raises(Forbidden, match="separation of duties"):
        svc.approve(ADMIN, p_admin.id)              # creator approving own plan
    svc.approve(CAROL, p_admin.id)


def test_production_can_never_be_a_target(svc):
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "bad", svc.tgt_id, svc.src_id)   # target = PRD
    prd = svc.adapters[svc.src_id]
    with pytest.raises(ProductionWriteBlocked):
        prd.upsert("KNA1", [])
    with pytest.raises(ProductionWriteBlocked):
        prd.set_number_level("SD_ORDER", 1)
    locked = SimulatedSap(SapSystem(sid="LK1", client="300", role="QAS", writable_target_allowed=False), build_source_dataset())
    lid = svc.register_system(ADMIN, locked.system, locked).id
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "locked", svc.src_id, lid)


def test_report_and_evidence_package(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    md = svc.report_markdown(run.id)
    assert "SIMULATED" in md and "RELEASED" in md and "Reconciliation" in md
    z = zipfile.ZipFile(io.BytesIO(svc.evidence_package(run.id)))
    names = set(z.namelist())
    assert {"manifest.json", "conflicts.json", "reconciliation.json", "audit.json", "report.md", "SHA256SUMS.json"} <= names
    sums = json.loads(z.read("SHA256SUMS.json"))["files"]
    import hashlib
    for n, h in sums.items():
        assert hashlib.sha256(z.read(n)).hexdigest() == h
    assert json.loads(z.read("audit_verification.json"))["valid"]

from rfactory.selective.conflicts import Action, ConflictType, validate_policy
from .conftest import ALICE, CAROL, ADMIN, make_project, ready_project


def findings(svc, p, t):
    return [f for f in p.report.findings if f.type == t]


def analyzed(svc, policy=None, **kw):
    p = make_project(svc, policy, **kw)
    svc.build_plan(ALICE, p.id)
    svc.analyze_conflicts(ALICE, p.id)
    return p


def test_default_policy_blocks_on_modified_target_objects(svc):
    p = analyzed(svc)
    dup = findings(svc, p, ConflictType.DUPLICATE_DIFFERENT)
    assert dup and all(f.severity == "blocking" for f in dup)
    assert p.report.blocking


def test_tester_created_documents_report_owner(svc):
    p = analyzed(svc)
    owned = [f for f in findings(svc, p, ConflictType.DUPLICATE_DIFFERENT) if f.details.get("owner")]
    assert owned and owned[0].details["owner"] == "qa.alice"


def test_identical_target_objects_are_skipped_not_reloaded(svc):
    p = analyzed(svc, {"DUPLICATE_DIFFERENT": "SKIP"})
    ident = findings(svc, p, ConflictType.DUPLICATE_IDENTICAL)
    assert len(ident) == 1 and ident[0].instance == "CUSTOMER:0000100001"
    assert p.report.decisions["CUSTOMER:0000100001"] == Action.SKIP
    assert "CUSTOMER:0000100001" in p.report.equivalent_in_target


def test_skipping_a_colliding_order_quarantines_its_whole_document_chain(svc):
    p = analyzed(svc, {"DUPLICATE_DIFFERENT": "SKIP"})
    assert not p.report.blocking
    skipped_orders = [i for i, a in p.report.decisions.items() if i.startswith("SALES_ORDER") and a == Action.SKIP]
    assert skipped_orders
    for oid in skipped_orders:
        for iid, inst in p.plan.instances.items():
            if oid in inst.requires:  # deliveries / billing hanging off the skipped order
                assert p.report.decisions[iid] == Action.QUARANTINE, f"{iid} would attach to a different target order"
    assert findings(svc, p, ConflictType.CHAIN_INCONSISTENT)


def test_missing_plant_customizing_quarantines_cross_company_order_only(svc):
    p = analyzed(svc, {"DUPLICATE_DIFFERENT": "SKIP"})
    cfg = findings(svc, p, ConflictType.CONFIG_MISSING)
    docs = [f for f in cfg if f.instance.startswith(("SALES_ORDER", "DELIVERY"))]
    assert docs and all(p.report.decisions[f.instance] == Action.QUARANTINE for f in docs)
    masters = [f for f in cfg if f.instance.startswith("MATERIAL")]
    assert masters and all(p.report.decisions[f.instance] == Action.LOAD for f in masters), "materials still load"
    assert all(f.instance in p.report.row_exclusions for f in masters)  # only the plant-2000 MARC rows are dropped


def test_number_range_adjustments(svc):
    p = analyzed(svc, {"DUPLICATE_DIFFERENT": "SKIP"})
    nr = findings(svc, p, ConflictType.NUMBER_RANGE)
    assert {f.details["object"] for f in nr} == set(p.report.number_range_adjustments)
    assert "SD_ORDER" in p.report.number_range_adjustments
    loaded_max = max(int(p.plan.instances[i].rows["VBAK"][0]["VBELN"]) for i in p.report.executable if i.startswith("SALES_ORDER"))
    assert p.report.number_range_adjustments["SD_ORDER"] == loaded_max


def test_replace_requires_approved_exception(svc):
    p = analyzed(svc)
    victim = next(f.instance for f in findings(svc, p, ConflictType.DUPLICATE_DIFFERENT) if f.instance.startswith("SALES_ORDER"))
    svc.apply_conflict_policy(ALICE, p.id, {"DUPLICATE_DIFFERENT": "SKIP"}, {victim: "REPLACE"})
    svc.build_plan(ALICE, p.id); svc.analyze_conflicts(ALICE, p.id)
    assert p.report.decisions[victim] == Action.FAIL and p.report.blocking
    svc.approve_exception(CAROL, p.id, victim, "QA lead alice agreed in CHG-4711")
    svc.build_plan(ALICE, p.id); svc.analyze_conflicts(ALICE, p.id)
    assert p.report.decisions[victim] == Action.REPLACE and not p.report.blocking


def test_update_not_supported_for_documents_but_allowed_for_masters(svc):
    p = make_project(svc, {"DUPLICATE_DIFFERENT": "UPDATE"})
    svc.build_plan(ALICE, p.id); svc.analyze_conflicts(ALICE, p.id)
    for iid, a in p.report.decisions.items():
        if iid.startswith("SALES_ORDER") and any(f.instance == iid and f.type == ConflictType.DUPLICATE_DIFFERENT for f in p.report.findings):
            assert a == Action.FAIL
    assert p.report.decisions["CUSTOMER:0000100002"] == Action.UPDATE


def test_policy_validation_rejects_unimplemented_and_invalid_actions():
    assert validate_policy({"DUPLICATE_DIFFERENT": "REMAP"})
    assert validate_policy({"CONFIG_MISSING": "REPLACE"})
    assert validate_policy({"NOPE": "SKIP"})
    assert not validate_policy({"DUPLICATE_DIFFERENT": "SKIP", "CONFIG_MISSING": "QUARANTINE"})


def test_existing_target_data_is_never_touched_without_policy(svc):
    before = {k: [dict(r) for r in v] for k, v in svc.adapters[svc.tgt_id].data.items()}
    p = analyzed(svc)  # analysis only
    assert before == svc.adapters[svc.tgt_id].data

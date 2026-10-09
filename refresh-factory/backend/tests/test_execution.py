import copy

from rfactory.sap.adapter import TransientError
from .conftest import ALICE, approved_project, norm


def fault_after(n_ok, exc, times=1):
    state = {"calls": 0, "raised": 0}

    def inj(op, table):
        if op != "upsert":
            return
        state["calls"] += 1
        if state["calls"] > n_ok and state["raised"] < times:
            state["raised"] += 1
            raise exc
    return inj


def clean_result(svc_factory):
    s = svc_factory()
    p = approved_project(s)
    run = s.execute(ALICE, p.id)
    return s.adapters[s.tgt_id].data, run


def test_transient_errors_are_retried(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id, fault_injector=fault_after(5, TransientError("RFC timeout"), times=2))
    assert run.status == "COMPLETED" and run.reconciliation["release"] == "RELEASED"
    assert sum(1 for e in run.events if e["level"] == "warn") == 2


def test_permanent_failure_checkpoints_and_resume_matches_clean_run(svc, tmp_path, monkeypatch):
    monkeypatch.setenv("RF_MASKING_KEY", "k" * 32)  # same pseudonymization key in both services
    from rfactory.service import RefreshService
    from .conftest import ADMIN

    def build(path):
        s = RefreshService(path); b = s.bootstrap_demo(ADMIN); s.src_id, s.tgt_id = b["source"]["id"], b["target"]["id"]
        return s
    clean = build(tmp_path / "clean")
    pc = approved_project(clean)
    clean.execute(ALICE, pc.id)

    p = approved_project(svc)
    run = svc.execute(ALICE, p.id, fault_injector=fault_after(20, RuntimeError("lock table overflow")))
    assert run.status == "FAILED" and 0 < run.checkpoint < len(run.order)
    assert p.status == "FAILED" and run.reconciliation is None
    cp = run.checkpoint
    run = svc.resume(ALICE, run.id)
    assert run.status == "COMPLETED" and run.attempts == 2 and len(run.loaded) == len(run.order) and run.checkpoint >= cp
    assert run.reconciliation["release"] == "RELEASED"
    assert norm(svc.adapters[svc.tgt_id].data) == norm(clean.adapters[clean.tgt_id].data), "resume must converge to the clean-run state"


def test_rollback_restores_target_exactly(svc):
    tgt = svc.adapters[svc.tgt_id]
    before = copy.deepcopy(tgt.data)
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    assert tgt.data != before
    svc.rollback(ALICE, run.id)
    assert norm(tgt.data) == norm(before)
    assert run.status == "ROLLED_BACK" and run.release == "HELD" and p.status == "ROLLED_BACK"


def test_rollback_after_partial_failure(svc):
    tgt = svc.adapters[svc.tgt_id]
    before = copy.deepcopy(tgt.data)
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id, fault_injector=fault_after(30, RuntimeError("disk full")))
    assert run.status == "FAILED" and tgt.data != before
    svc.rollback(ALICE, run.id)
    assert norm(tgt.data) == norm(before)


def test_replace_decision_deletes_then_loads_and_rolls_back(svc):
    from .conftest import CAROL
    p = approved_project(svc)  # baseline to find a victim
    victim = next(f.instance for f in p.report.findings if f.type.value == "DUPLICATE_DIFFERENT" and f.instance.startswith("SALES_ORDER"))
    svc.apply_conflict_policy(ALICE, p.id, {}, {victim: "REPLACE"})
    svc.approve_exception(CAROL, p.id, victim, "owner consent CHG-1")
    svc.build_plan(ALICE, p.id)
    svc.analyze_conflicts(ALICE, p.id)
    svc.submit(ALICE, p.id); svc.approve(CAROL, p.id)
    tgt = svc.adapters[svc.tgt_id]
    key = victim.split(":")[1]
    assert tgt.get("VBAK", (key,))["ERNAM"] == "QA_ALICE"
    before = copy.deepcopy(tgt.data)
    run = svc.execute(ALICE, p.id)
    assert run.status == "COMPLETED", run.error
    assert tgt.get("VBAK", (key,))["ERNAM"] == "BATCHUSR"
    assert len(tgt.lookup("VBAP", "VBELN", key)) == len(p.plan.instances[victim].rows["VBAP"])  # old tester items gone
    svc.rollback(ALICE, run.id)
    assert norm(tgt.data) == norm(before)

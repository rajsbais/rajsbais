import copy
from datetime import datetime, timedelta, timezone

import pytest

from rfactory.masking.engine import MaskingEngine
from rfactory.sap.adapter import ChangeLogGap, TransientError
from rfactory.sap.ddic import TABLES
from rfactory.sap.synthetic import simulate_business_activity
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict
from rfactory.selective.manifest import Scope
from .conftest import ADMIN, ALICE, BOB, CAROL, norm

SCHED = U["svc.scheduler"]


def spec(svc, family="ECC", **over):
    s, t = (svc.s4_src, svc.s4_tgt) if family == "S4" else (svc.src_id, svc.tgt_id)
    base = {"name": "weekend sync", "source_id": s, "target_id": t,
            "scopes": [{"scope": Scope(object_type="CUSTOMER", company_codes=["1000"])},
                       {"scope": Scope(object_type="VENDOR", company_codes=["1000"])},
                       {"scope": Scope(object_type="MATERIAL", plants=["1000", "1010"])},
                       {"scope": Scope(object_type="SALES_ORDER", company_codes=["1000"]), "rolling_days": 90},
                       {"scope": Scope(object_type="PURCHASE_ORDER", company_codes=["1000"]), "rolling_days": 90}],
            "include_downstream": ["DELIVERY", "BILLING", "FI_DOCUMENT"], "masking_policy_id": "gdpr-standard",
            "conflict_policy": {"DUPLICATE_DIFFERENT": "SKIP"},
            "schedule": {"kind": "weekly", "weekday": 5, "hour": 2, "window_hours": 4}, "full_sweep_every": 0}
    base.update(over)
    return base


def approved(svc, family="ECC", **over):
    sc = svc.delta.create(ALICE, spec(svc, family, **over))
    # the pseudonymization key must match across a test's services only when comparing; fixed here for determinism
    sc.mask_key = b"k" * 32
    # standing masking coverage for the custom Z field
    from rfactory.masking.engine import Rule
    sc.masking_policy.rules.append(Rule("KNA1", "ZZ_CONTACT_EMAIL", "EMAIL", "email"))
    svc.delta.submit(ALICE, sc.id)
    svc.delta.approve(CAROL, sc.id, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    return sc


def src_of(svc, family="ECC"):
    return svc.adapters[svc.s4_src if family == "S4" else svc.src_id]


def tgt_of(svc, family="ECC"):
    return svc.adapters[svc.s4_tgt if family == "S4" else svc.tgt_id]


def pseudo(sc, category_value, rule_table, rule_field):
    eng = MaskingEngine(sc.masking_policy, persistent_key=sc.mask_key)
    rule = next(r for r in sc.masking_policy.rules if (r.table, r.field) == (rule_table, rule_field))
    return eng.mask_value(rule, category_value)


# ---------------------------------------------------------------- initial + incremental
def test_initial_copy_sets_watermark_tracks_objects_and_gates(svc):
    sc = approved(svc)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["kind"] == "initial" and rec["status"] == "COMPLETED" and rec["release"] == "RELEASED", rec
    assert sc.watermark == src_of(svc).change_seq() and len(sc.tracked) == rec["loaded"] > 0
    assert rec["version"] == 1 and rec["new"] == rec["scope_objects"]
    assert tgt_of(svc).count("KNA1") > 0 and tgt_of(svc).count("EKKO") > 0  # multi-object scope incl. purchasing documents


def test_no_changes_means_no_writes(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    before = copy.deepcopy(tgt_of(svc).data)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["kind"] == "delta" and rec["new"] == 0 and rec["changed"] == 0 and rec["loaded"] == 0
    assert tgt_of(svc).data == before and rec["version"] == 2


@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_delta_picks_up_new_changed_and_dependencies(svc, family):
    sc = approved(svc, family)
    svc.delta.run(ALICE, sc.id)
    ev = simulate_business_activity(src_of(svc, family))
    pv = svc.delta.preview(ALICE, sc.id)
    assert pv["new"] >= 3 and pv["changed"] >= 3 and pv["mode"] == "change_log" and pv["blocking"] == []
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["status"] == "COMPLETED" and rec["release"] == "RELEASED", rec
    tgt = tgt_of(svc, family)
    # new order arrived together with its (new) customer: dependency-complete
    assert tgt.get("VBAK", (ev["new_order"],)) and tgt.get("KNA1", (ev["new_customer"],)) and tgt.get("EKKO", (ev["new_po"],))
    if family == "S4":
        assert tgt.get("BUT000", (ev["new_customer"],))
    # changed order: quantity updated AND the removed item is gone from the target
    items = tgt.lookup("VBAP", "VBELN", ev["changed_order"])
    assert ev["dropped_item"] not in {i["POSNR"] for i in items}
    assert {i["POSNR"] for i in items} == {i["POSNR"] for i in src_of(svc, family).lookup("VBAP", "VBELN", ev["changed_order"])}
    # PII change propagated with the SAME pseudonym in every table
    exp = pseudo(sc, ev["new_name"], "KNA1", "NAME1")
    kna = tgt.get("KNA1", (ev["renamed_customer"],))
    adr = tgt.get("ADRC", (src_of(svc, family).get("KNA1", (ev["renamed_customer"],))["ADRNR"],))
    assert kna["NAME1"] == exp == adr["NAME1"] and ev["new_name"] not in str(tgt.data)
    assert sc.watermark == ev["change_seq"]
    # nothing is reloaded twice
    assert svc.delta.run(ALICE, sc.id)["loaded"] == 0


def test_only_affected_objects_are_loaded(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    simulate_business_activity(src_of(svc))
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["loaded"] <= 15 and rec["loaded"] < len(sc.tracked) / 2


# ---------------------------------------------------------------- failure / watermark safety
def test_failed_run_keeps_watermark_resumes_to_same_state(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    wm = sc.watermark
    simulate_business_activity(src_of(svc))
    n = {"c": 0}

    def inj(op, table):
        if op == "upsert":
            n["c"] += 1
            if n["c"] == 6:
                raise RuntimeError("lock table overflow")
    rec = svc.delta.run(ALICE, sc.id, fault_injector=inj)
    assert rec["status"] == "FAILED" and sc.status == "FAILED" and sc.watermark == wm
    with pytest.raises(Conflict, match="resume, roll back"):
        svc.delta.run(ALICE, sc.id)
    rec = svc.delta.resume(ALICE, sc.id)
    assert rec["status"] == "COMPLETED" and rec["release"] == "RELEASED" and sc.status == "APPROVED" and sc.watermark > wm

    # converges to exactly what an uninterrupted delta produces
    from rfactory.service import RefreshService
    clean = RefreshService(svc.data_dir / "clean"); b = clean.bootstrap_demo(ADMIN)
    clean.src_id, clean.tgt_id, clean.s4_src, clean.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    c = approved(clean); clean.delta.run(ALICE, c.id); simulate_business_activity(src_of(clean)); clean.delta.run(ALICE, c.id)
    assert norm(tgt_of(svc).data) == norm(tgt_of(clean).data)


def test_transient_errors_retried_inside_delta(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    simulate_business_activity(src_of(svc))
    n = {"c": 0}

    def inj(op, table):
        if op == "upsert":
            n["c"] += 1
            if n["c"] in (2, 3):
                raise TransientError("RFC timeout")
    assert svc.delta.run(ALICE, sc.id, fault_injector=inj)["status"] == "COMPLETED"


def test_rollback_restores_target_and_tracking(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    before, tracked, wm = copy.deepcopy(tgt_of(svc).data), set(sc.tracked), sc.watermark
    simulate_business_activity(src_of(svc))

    def inj(op, table):
        if op == "upsert" and tgt_of(svc).count("KNA1") > 0 and table == "EKPO":
            raise RuntimeError("disk full")
    rec = svc.delta.run(ALICE, sc.id, fault_injector=inj)
    assert rec["status"] == "FAILED" and norm(tgt_of(svc).data) != norm(before)
    svc.delta.rollback(ALICE, sc.id)
    assert norm(tgt_of(svc).data) == norm(before) and set(sc.tracked) == tracked and sc.watermark == wm and sc.status == "APPROVED"
    assert svc.delta.run(ALICE, sc.id)["status"] == "COMPLETED"  # the same delta re-applies cleanly


def test_held_gate_blocks_watermark_until_acknowledged(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    wm = sc.watermark
    simulate_business_activity(src_of(svc))
    tgt_of(svc).outbound_interfaces()[0]["active"] = True  # security gate will fail
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["release"] == "HELD" and sc.status == "ATTENTION" and sc.watermark == wm
    with pytest.raises(Conflict):
        svc.delta.run(ALICE, sc.id)
    with pytest.raises(Conflict, match="note"):
        svc.delta.acknowledge(ALICE, sc.id, " ")
    with pytest.raises(Forbidden):
        svc.delta.acknowledge(U["refresh.copilot"], sc.id, "ok")
    svc.delta.acknowledge(ALICE, sc.id, "interface disabled in the next job, CHG-77")
    assert sc.status == "APPROVED" and sc.watermark > wm


# ---------------------------------------------------------------- conflicts specific to delta
def edit_target_customer(svc, kunnr):
    tgt_of(svc).get("KNA1", (kunnr,))["LAND1"] = "CH"  # a tester edits a loaded customer


def test_target_drift_blocks_by_default(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    ev = simulate_business_activity(src_of(svc))
    edit_target_customer(svc, ev["renamed_customer"])
    pv = svc.delta.preview(ALICE, sc.id)
    assert pv["target_drift"] == 1 and any("changed in the target" in b for b in pv["blocking"])
    before = copy.deepcopy(tgt_of(svc).data); wm = sc.watermark
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["status"] == "BLOCKED" and tgt_of(svc).data == before and sc.watermark == wm


def test_target_drift_policies_skip_and_replace(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    ev = simulate_business_activity(src_of(svc))
    edit_target_customer(svc, ev["renamed_customer"])
    sc.conflict_policy["TARGET_DRIFT"] = "SKIP"; sc.approval = None; sc.status = "DRAFT"
    svc.delta.submit(ALICE, sc.id); svc.delta.approve(CAROL, sc.id)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["status"] == "COMPLETED"
    assert tgt_of(svc).get("KNA1", (ev["renamed_customer"],))["LAND1"] == "CH"                       # tester edit kept
    assert tgt_of(svc).get("KNA1", (ev["renamed_customer"],))["NAME1"] != pseudo(sc, ev["new_name"], "KNA1", "NAME1")
    sc.conflict_policy["TARGET_DRIFT"] = "REPLACE"; sc.approval = None; sc.status = "DRAFT"
    svc.delta.submit(ALICE, sc.id); svc.delta.approve(CAROL, sc.id)
    svc.delta.run(ALICE, sc.id)
    k = tgt_of(svc).get("KNA1", (ev["renamed_customer"],))
    assert k["LAND1"] == src_of(svc).get("KNA1", (ev["renamed_customer"],))["LAND1"] and k["NAME1"] == pseudo(sc, ev["new_name"], "KNA1", "NAME1")


def test_duplicate_prevention_new_source_order_colliding_with_tester_order(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    ev = simulate_business_activity(src_of(svc))
    tgt = tgt_of(svc)  # a tester used the same document number meanwhile
    tgt.data["VBAK"].append({"VBELN": ev["new_order"], "ERDAT": "2026-09-01", "AUART": "OR", "VKORG": "1000", "BUKRS_VF": "1000",
                             "KUNNR": "0000100001", "NETWR": 1.0, "WAERK": "EUR", "ERNAM": "QA_BOB"}); tgt._idx.clear()
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["status"] == "COMPLETED"
    assert tgt.get("VBAK", (ev["new_order"],))["ERNAM"] == "QA_BOB"          # not overwritten
    assert f"SALES_ORDER:{ev['new_order']}" not in sc.tracked                # not claimed
    assert sc.tracked  # the rest of the delta was still applied


def test_new_sensitive_field_blocks_until_covered(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    src = src_of(svc)
    src.sim_update("KNA1", (src.data["KNA1"][0]["KUNNR"],), STCD1="DE123456789")
    src.data["KNA1"][0]["LAND1"] = "DE"
    sc.masking_policy.rules = [r for r in sc.masking_policy.rules if r.field != "ZZ_CONTACT_EMAIL"]
    pv = svc.delta.preview(ALICE, sc.id)
    assert any("no masking rule" in b for b in pv["blocking"])


# ---------------------------------------------------------------- mechanisms, sweeps, windows
def modify_billing(svc, sc):
    src = src_of(svc)
    bill = next(r for r in src.data["VBRK"] if f"BILLING:{r['VBELN']}" in sc.tracked)
    src.sim_update("VBRK", (bill["VBELN"],), WAERK="USD")
    return bill["VBELN"]


def test_created_only_objects_ignore_changes_until_full_sweep(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    vb = modify_billing(svc, sc)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["changed"] == 0 and rec["ignored_modifications"] >= 1 and any("immutable" in n for n in rec["notes"])
    assert tgt_of(svc).get("VBRK", (vb,))["WAERK"] == "EUR"
    rec = svc.delta.run(ALICE, sc.id, full_sweep=True)
    assert rec["mode"] == "full_sweep" and rec["changed"] == 1 and tgt_of(svc).get("VBRK", (vb,))["WAERK"] == "USD"


def test_scheduled_full_sweep_every_n_runs(svc):
    sc = approved(svc, full_sweep_every=3)
    svc.delta.run(ALICE, sc.id); vb = modify_billing(svc, sc)
    assert svc.delta.run(ALICE, sc.id)["mode"] == "change_log"
    rec = svc.delta.run(ALICE, sc.id)  # run 3
    assert rec["mode"] == "full_sweep" and tgt_of(svc).get("VBRK", (vb,))["WAERK"] == "USD"


def test_change_log_gap_forces_full_sweep(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    simulate_business_activity(src_of(svc))
    src_of(svc).sim_purge_log()  # change documents archived before we read them
    with pytest.raises(ChangeLogGap):
        src_of(svc).changes_since(sc.watermark)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["mode"] == "full_sweep" and any("gap" in n for n in rec["notes"]) and rec["new"] >= 3 and rec["release"] == "RELEASED"


def test_rolling_window_never_deletes_from_target(svc):
    sc = approved(svc, scopes=spec(svc)["scopes"][:3] + [
        {"scope": Scope(object_type="SALES_ORDER", company_codes=["1000"]), "rolling_days": 30}])
    svc.delta.run(ALICE, sc.id)
    n_orders = tgt_of(svc).count("VBAK")
    src_of(svc).ref_date = src_of(svc).ref_date + timedelta(days=45)  # time passes: old orders leave the window
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["retained_out_of_scope"] > 0 and tgt_of(svc).count("VBAK") == n_orders


def test_source_deletion_is_reported_not_propagated(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id)
    src = src_of(svc)
    po = next(r for r in src.data["EKKO"] if f"PURCHASE_ORDER:{r['EBELN']}" in sc.tracked)
    for r in src.lookup("EKPO", "EBELN", po["EBELN"]):
        src.sim_delete("EKPO", (r["EBELN"], r["EBELP"]))
    src.sim_delete("EKKO", (po["EBELN"],))
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["deleted_in_source"] == 1 and tgt_of(svc).get("EKKO", (po["EBELN"],)) is not None


def test_workload_throttle(svc):
    sc = approved(svc, max_objects=5)
    rec = svc.delta.run(ALICE, sc.id)
    assert rec["status"] == "BLOCKED" and any("max_objects" in b for b in rec["blocking"]) and tgt_of(svc).count("KNA1") == 2


# ---------------------------------------------------------------- governance
def test_approval_required_sod_and_reapproval_on_change(svc):
    sc = svc.delta.create(ALICE, spec(svc))
    with pytest.raises(Conflict, match="approved"):
        svc.delta.run(ALICE, sc.id)
    svc.delta.submit(ALICE, sc.id)
    for who in (ALICE, U["refresh.copilot"]):
        with pytest.raises(Forbidden):
            svc.delta.approve(who, sc.id)
    svc.delta.approve(CAROL, sc.id)
    svc.delta.update(ALICE, sc.id, {"schedule": {"kind": "interval", "every_hours": 24}})  # schedule is not part of the hash
    assert sc.status == "APPROVED"
    svc.delta.update(ALICE, sc.id, {"conflict_policy": {"DUPLICATE_DIFFERENT": "UPDATE"}})
    assert sc.status == "DRAFT" and sc.approval is None and sc.config_version == 2
    with pytest.raises(Conflict):
        svc.delta.run(ALICE, sc.id)


def test_creation_guards(svc):
    with pytest.raises(Forbidden):
        svc.delta.create(ALICE, spec(svc, source_id=svc.tgt_id, target_id=svc.src_id))      # production target
    with pytest.raises(Conflict, match="stable masking"):
        svc.delta.create(ALICE, spec(svc, masking_policy_id="gdpr-strict"))                  # per-run keys break consistency
    with pytest.raises(Conflict, match="migration"):
        svc.delta.create(ALICE, spec(svc, target_id=svc.s4_tgt))
    with pytest.raises(Conflict):
        svc.delta.create(ALICE, spec(svc, conflict_policy={"DUPLICATE_DIFFERENT": "REMAP"}))
    with pytest.raises(Conflict):
        svc.delta.create(ALICE, spec(svc, schedule={"kind": "weekly", "weekday": 9, "hour": 2}))


# ---------------------------------------------------------------- scheduling
def test_scheduler_runs_due_scenarios_inside_window_only(svc):
    sc = approved(svc)  # approved Wed 2026-10-07 12:00 UTC -> next Saturday 02:00
    assert sc.next_due == datetime(2026, 10, 10, 2, tzinfo=timezone.utc)
    assert svc.delta.tick(SCHED, datetime(2026, 10, 9, 12, tzinfo=timezone.utc)) == []          # not due
    with pytest.raises(Forbidden):
        svc.delta.tick(U["erin.auditor"])
    out = svc.delta.tick(SCHED, datetime(2026, 10, 10, 3, tzinfo=timezone.utc))                  # inside window
    assert out[0]["action"] == "ran" and out[0]["release"] == "RELEASED" and sc.history[-1]["trigger"] == "schedule"
    assert sc.next_due == datetime(2026, 10, 17, 3, tzinfo=timezone.utc).replace(hour=2) or sc.next_due.weekday() == 5
    sc.next_due = datetime(2026, 10, 17, 2, tzinfo=timezone.utc)
    out = svc.delta.tick(SCHED, datetime(2026, 10, 17, 9, tzinfo=timezone.utc))                  # window (4h) missed
    assert out[0]["action"] == "missed_window" and len(sc.history) == 1
    assert sc.next_due == datetime(2026, 10, 24, 2, tzinfo=timezone.utc)


def test_scheduler_does_not_run_scenarios_needing_attention(svc):
    sc = approved(svc)
    sc.status = "ATTENTION"; sc.next_due = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)
    out = svc.delta.tick(SCHED, datetime(2026, 10, 10, 3, tzinfo=timezone.utc))
    assert out and out[0]["action"] == "skipped" and not sc.history


def test_history_is_versioned_and_audited(svc):
    sc = approved(svc)
    svc.delta.run(ALICE, sc.id); simulate_business_activity(src_of(svc)); svc.delta.run(ALICE, sc.id)
    assert [h["version"] for h in sc.history] == [1, 2]
    assert sc.history[1]["from_seq"] == sc.history[0]["to_seq"]
    acts = [e["action"] for e in svc.audit.entries()]
    for a in ("delta.created", "delta.approved", "delta.run.started", "delta.reconciliation"):
        assert a in acts
    assert svc.audit.verify()["valid"]

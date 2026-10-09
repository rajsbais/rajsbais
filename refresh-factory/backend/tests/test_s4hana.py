"""S/4HANA coverage: Business Partner + ACDOCA object model, masking, reconciliation, family guards."""
import pytest

from rfactory.security.auth import Forbidden
from rfactory.service import Conflict
from .conftest import ADMIN, ALICE, CAROL, approved_project, make_project


def s4_project(svc, **kw):
    return approved_project(svc, src=svc.s4_src, tgt=svc.s4_tgt, **kw)


def test_families_detected_and_registries_differ(svc):
    assert svc.system(svc.src_id).family == "ECC" and svc.system(svc.s4_src).family == "S4"
    ecc, s4 = svc.registries["ECC"], svc.registries["S4"]
    assert "BUT000" not in [l.table for l in ecc.types["CUSTOMER"].tables]
    assert "BUT000" in [l.table for l in s4.types["CUSTOMER"].tables]
    assert "ACDOCA" in [l.table for l in s4.types["FI_DOCUMENT"].tables] and s4.validate()["ok"]


def test_s4_discovery_and_readiness(svc):
    d = svc.discover(svc.s4_src)
    assert d["family"] == "S4" and "S/4HANA" in d["system"]["product"] and d["table_counts"]["BUT000"] > 0 and d["table_counts"]["ACDOCA"] > 0
    assert svc.readiness(svc.s4_tgt)["ready"]


def test_s4_plan_pulls_business_partner_and_universal_journal(svc):
    p = make_project(svc, src=svc.s4_src, tgt=svc.s4_tgt)
    plan = svc.build_plan(ALICE, p.id)
    assert not plan["blocking"] and plan["tables"]["BUT000"] > 0 and plan["tables"]["ACDOCA"] > 0
    assert plan["tables"]["ACDOCA"] == plan["tables"]["BSEG"]


def test_s4_end_to_end_released_and_masked(svc):
    p = s4_project(svc)
    run = svc.execute(ALICE, p.id)
    assert run.status == "COMPLETED", run.error
    rec = run.reconciliation
    ids = {c["id"] for c in rec["checks"]}
    assert {"BUS-ACDOCA", "BUS-BP", "TECH-COUNT-BUT000", "TECH-COUNT-ACDOCA"} <= ids
    assert rec["release"] == "RELEASED", [c for c in rec["checks"] if c["status"] != "pass"]
    src, tgt = svc.adapters[svc.s4_src], svc.adapters[svc.s4_tgt]
    cust = next(i for i in run.loaded if i.startswith("CUSTOMER")).split(":")[1]
    bp, kna = tgt.get("BUT000", (cust,)), tgt.get("KNA1", (cust,))
    assert bp["NAME_ORG1"] != src.get("BUT000", (cust,))["NAME_ORG1"]
    assert bp["NAME_ORG1"] == kna["NAME1"], "BP name and KNA1 name stay consistent after masking"
    assert bp["BU_SORT1"] != src.get("BUT000", (cust,))["BU_SORT1"]  # search term derived from the name is masked too


def test_s4_reconciliation_detects_journal_drift_and_missing_bp(svc):
    p = s4_project(svc)
    run = svc.execute(ALICE, p.id)
    tgt = svc.adapters[svc.s4_tgt]
    fi = next(i for i in run.loaded if i.startswith("FI_DOCUMENT"))
    line = p.plan.instances[fi].rows["ACDOCA"][0]
    tgt.get("ACDOCA", tuple(line[k] for k in ("RLDNR", "RBUKRS", "GJAHR", "BELNR", "DOCLN")))["HSL"] += 3
    cust = next(i for i in run.loaded if i.startswith("CUSTOMER")).split(":")[1]
    tgt.delete("BUT000", (cust,))
    svc._after_run(p, run)
    checks = {c["id"]: c for c in run.reconciliation["checks"]}
    assert checks["BUS-ACDOCA"]["status"] == "fail" and checks["BUS-BP"]["status"] == "fail" and run.release == "HELD"


def test_s4_rollback_restores_target(svc):
    import copy
    from .conftest import norm
    tgt = svc.adapters[svc.s4_tgt]
    before = copy.deepcopy(tgt.data)
    p = s4_project(svc)
    run = svc.execute(ALICE, p.id)
    svc.rollback(ALICE, run.id)
    assert norm(tgt.data) == norm(before)


def test_ecc_to_s4_is_not_a_refresh(svc):
    with pytest.raises(Conflict, match="migration"):
        svc.create_project(ALICE, "x", svc.src_id, svc.s4_tgt)
    combo = next(c for c in svc.refresh_combinations() if c["source"] == svc.src_id and c["target"] == svc.s4_tgt)
    assert not combo["selective"] and not combo["full_system_refresh"] and combo["note"]
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "prd", svc.s4_tgt, svc.s4_src)


def test_s4_source_integrity_clean(svc):
    from rfactory.dependency.planner import Planner
    r = Planner(svc.source_view(svc.s4_src), svc.registries["S4"]).validate_relationships()
    assert r["orphans"] == 0 and r["dangling"] == 0

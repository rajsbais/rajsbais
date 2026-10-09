import copy
from datetime import datetime, timedelta, timezone

import pytest

from rfactory.leanclient.profiles import PURPOSES, preset
from rfactory.sap.ddic import TABLES
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict
from .conftest import ALICE, CAROL, norm

TINA, COPILOT = U["tina.tester"], U["refresh.copilot"]
T0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
CLIENTS = {"sandbox": "300", "training": "310", "functional": "320", "regression": "330"}


def ids(svc, family="ECC"):
    return (svc.s4_src, svc.s4_tgt) if family == "S4" else (svc.src_id, svc.tgt_id)


def template(svc, purpose="sandbox", family="ECC", approve=True, **over):
    spec = {**preset(purpose), "source_id": ids(svc, family)[0], **over}
    t = svc.lean.create_template(ALICE, spec)
    if approve:
        svc.lean.submit_template(ALICE, t.id)
        svc.lean.approve_template(CAROL, t.id)
    return t


def build(svc, t, client=None, family="ECC", **kw):
    spec = {"template_id": t.id, "host_id": ids(svc, family)[1], "client": client or CLIENTS[t.purpose], **kw.pop("spec", {})}
    return svc.lean.build(ALICE, spec, now=T0, **kw)


# ------------------------------------------------------------------ catalogs and templates
def test_profiles_and_workflows_are_distinct_and_honest(svc):
    c = svc.lean.profiles()
    assert {"SHELL", "CUSTOMIZING_ONLY", "CUSTOMIZING_PLUS_MASTER", "LEAN_TEST", "TRAINING"} <= {p["id"] for p in c["profiles"]}
    wf = {w["id"]: w for w in c["workflows"]}
    assert set(wf) == {"client_build", "selective_copy", "system_copy"}
    assert "catalogued" in wf["system_copy"]["implemented"] and set(c["reserved_clients"]) == {"000", "001", "066"}


@pytest.mark.parametrize("purpose", sorted(PURPOSES))
def test_every_preset_creates_a_valid_template(svc, purpose):
    t = template(svc, purpose, approve=False)
    assert t.public()["role"] == PURPOSES[purpose] and any(r["field"] == "ZZ_CONTACT_EMAIL" for r in t.masking_rules)


def test_template_validation(svc):
    with pytest.raises(Conflict, match="MATERIAL"):
        template(svc, "training", approve=False, masters=[{"type": "CUSTOMER", "max": 3}])      # synthetic needs materials
    with pytest.raises(Conflict, match="unknown company"):
        template(svc, "sandbox", approve=False, customizing={"company_codes": ["9999"]})
    with pytest.raises(Conflict, match="not supported"):
        template(svc, "sandbox", approve=False, transactions=[{"template_id": "r2r_billing_posting", "count": 1, "mode": "synthetic"}])
    with pytest.raises(Conflict, match="belongs in 'masters'"):
        template(svc, "sandbox", approve=False, transactions=[{"template_id": "md_materials", "count": 1, "mode": "subset"}])
    with pytest.raises(Conflict):
        template(svc, "sandbox", approve=False, masters=[{"type": "SALES_ORDER", "max": 3}])
    with pytest.raises(Conflict, match="purpose"):
        svc.lean.create_template(ALICE, {**preset("sandbox"), "source_id": svc.src_id, "purpose": "production"})


def test_template_governance_and_tamper_evidence(svc):
    t = template(svc, "sandbox", approve=False)
    with pytest.raises(Conflict, match="approved"):
        build(svc, t)
    svc.lean.submit_template(ALICE, t.id)
    for who in (ALICE, COPILOT, TINA):
        with pytest.raises(Forbidden):
            svc.lean.approve_template(who, t.id)
    svc.lean.approve_template(CAROL, t.id)
    t.masters.append({"type": "CUSTOMER", "max": 99})  # edited after approval
    with pytest.raises(Conflict, match="approved for its current definition"):
        build(svc, t)


# ------------------------------------------------------------------ estimate
def test_estimate_is_read_only_and_compares_with_a_full_client_copy(svc):
    t = template(svc, "functional")
    before_sys, before_tgt = set(svc.systems), copy.deepcopy(svc.adapters[svc.tgt_id].data)
    e = svc.lean.estimate(ALICE, t.id, svc.tgt_id)
    assert set(svc.systems) == before_sys and svc.adapters[svc.tgt_id].data == before_tgt
    assert 0 < e["lean"]["rows"] < e["full_client_copy"]["rows"] and e["savings"]["rows_pct"] > 50 and e["savings"]["bytes_pct"] > 50
    assert e["duration"]["lean"]["seconds"] < e["duration"]["full_client_copy"]["seconds"] and e["duration"]["lean"]["model_assumption"]
    assert e["lean"]["customizing_rows"] > 0 and "PLANT:2000" in e["customizing_pulled_in_by_data"]
    assert {w["id"] for w in e["workflows"]} == {"client_build", "selective_copy", "system_copy"} and e["caveats"]
    assert e["within_max_rows"]


def test_estimate_rejects_unbuildable_templates_and_bad_hosts(svc):
    t = template(svc, "sandbox", masters=[{"type": "CUSTOMER", "max": 3}], transactions=[])
    with pytest.raises(Forbidden):
        svc.lean.estimate(ALICE, t.id, svc.src_id)                       # production host
    with pytest.raises(Conflict, match="migration"):
        svc.lean.estimate(ALICE, t.id, svc.s4_tgt)                       # family mismatch
    t2 = template(svc, "sandbox", masters=[{"type": "VENDOR", "max": 2}], transactions=[], customizing={"company_codes": ["2000"]})
    assert svc.lean.estimate(ALICE, t2.id, svc.tgt_id)["lean"]["rows"] > 0


# ------------------------------------------------------------------ builds
def test_sandbox_build_customizing_masters_masking_and_integrity(svc):
    t = template(svc, "sandbox")
    src_before = copy.deepcopy(svc.adapters[svc.src_id].data)
    b = build(svc, t)
    assert b.status == "READY", b.reasons
    assert [p["name"] for p in b.phases] == ["PLAN", "SHELL", "CUSTOMIZING", "DATA", "VALIDATE", "FINALIZE"] and all(p["status"] == "DONE" for p in b.phases)
    s = svc.system(b.system_id); a = svc.adapters[b.system_id]
    assert (s.sid, s.client, s.role.value) == ("EQ1", "300", "SBX") and not s.is_production and s.writable_target_allowed
    assert {r["BUKRS"] for r in a.data["T001"]} == {"1000"} and {r["WERKS"] for r in a.data["T001W"]} == {"1000", "1010"}
    assert all(r["NRLEVEL"] == r["FROMNUMBER"] for r in a.data["NRIV"])
    assert 0 < a.count("KNA1") <= 3 and 0 < a.count("MARA") <= 4 and a.count("VBAK") == 0 and a.count("BKPF") == 0
    assert all(c["status"] == "pass" for c in b.checks) and {c["id"] for c in b.checks} >= {"L2-INTEGRITY", "L4-NUMBER-RANGES", "L5-NO-SOURCE-PII"}
    assert b.actual["rows"] == b.estimate["lean"]["rows"]                      # deterministic plan: estimate equals reality
    assert svc.adapters[svc.src_id].data == src_before                         # production source untouched
    blob = str(a.data)
    for k in svc.adapters[svc.src_id].data["KNA1"]:
        assert k["NAME1"] not in blob and k["STCD1"] not in blob
    assert svc.readiness(b.system_id)["ready"] and b.expires_at == T0 + timedelta(days=30)


def test_functional_build_slices_pulls_in_customizing_and_protects_numbers(svc):
    b = build(svc, template(svc, "functional"))
    assert b.status == "READY", b.reasons
    a = svc.adapters[b.system_id]
    assert a.count("VBAK") >= 3 and a.count("LIKP") >= 3 and a.count("VBRK") >= 1 and a.count("BKPF") >= 1 and a.count("EKKO") >= 2
    assert {"PLANT:2000", "COMPANY_CODE:2000"} <= set(b.estimate["customizing_pulled_in_by_data"])
    assert a.get("T001W", ("2000",)) and a.get("T001", ("2000",))               # cross-company order's plant brought its customizing
    assert a.number_level("SD_ORDER") >= max(int(r["VBELN"]) for r in a.data["VBAK"])
    assert b.actual["rows"] <= b.estimate["lean"]["rows"] * 1.0 + 0 and b.actual["rows"] == b.estimate["lean"]["rows"]
    assert svc.runs[b.runs[0]].reconciliation["release"] == "RELEASED"
    p = svc.create_project(ALICE, "refresh into the new client", svc.src_id, b.system_id)   # a writable target for other modules
    assert p.target_id == b.system_id


def test_training_client_has_only_synthetic_documents_and_is_protected(svc):
    b = build(svc, template(svc, "training"))
    assert b.status == "READY", b.reasons
    a = svc.adapters[b.system_id]
    assert a.count("VBAK") >= 5 and {r["ERNAM"] for r in a.data["VBAK"]} == {"TDM_SYNTH"} and a.count("EKKO") >= 2
    assert abs(b.actual["rows"] - b.estimate["lean"]["rows"]) <= 0.3 * b.estimate["lean"]["rows"]  # synthetic slices vary
    assert not svc.system(b.system_id).writable_target_allowed and svc.system(b.system_id).role.value == "TRN"
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "overwrite training", svc.src_id, b.system_id)
    svc.lean.set_protection(ALICE, b.system_id, locked=False)
    svc.create_project(ALICE, "now allowed", svc.src_id, b.system_id)
    svc.lean.set_protection(ALICE, b.system_id, locked=True)
    with pytest.raises(Forbidden):
        svc.lean.set_protection(TINA, b.system_id, locked=False)


def test_regression_client_covers_all_scenarios(svc):
    b = build(svc, template(svc, "regression"))
    assert b.status == "READY", b.reasons
    a = svc.adapters[b.system_id]
    assert a.count("BKPF") >= 2 and a.count("EKKO") >= 3 and a.count("VBAK") >= 8
    assert b.estimate["savings"]["rows_pct"] > 50 and b.status == "READY"


@pytest.mark.parametrize("purpose", ["sandbox", "functional", "training"])
def test_s4hana_builds_business_partner_and_universal_journal(svc, purpose):
    b = build(svc, template(svc, purpose, "S4"), family="S4", client="400")
    assert b.status == "READY", b.reasons
    a = svc.adapters[b.system_id]
    assert svc.system(b.system_id).family == "S4" and a.count("BUT000") > 0
    if purpose != "sandbox":
        assert a.count("ACDOCA") > 0
    assert all(c["status"] == "pass" for c in b.checks)


# ------------------------------------------------------------------ guards, failure, rollback
def test_client_number_and_host_guards(svc):
    t = template(svc, "sandbox")
    for bad, msg in (("20", "3-digit"), ("ABC", "3-digit"), ("000", "delivery client"), ("001", "delivery client"), ("066", "delivery client"),
                     ("200", "already exists")):
        with pytest.raises(Conflict, match=msg):
            build(svc, t, client=bad)
    with pytest.raises(Forbidden):
        svc.lean.build(ALICE, {"template_id": t.id, "host_id": svc.src_id, "client": "300"})
    with pytest.raises(Forbidden):
        svc.lean.build(TINA, {"template_id": t.id, "host_id": svc.tgt_id, "client": "300"})
    assert build(svc, t, client="300").status == "READY"
    with pytest.raises(Conflict, match="already exists"):
        build(svc, t, client="300")
    assert build(svc, t, client="301").status == "READY"


def test_failed_build_removes_the_half_built_client_and_can_be_retried(svc):
    t = template(svc, "functional")
    systems = set(svc.systems)
    calls = {"n": 0}

    def boom(op, table):
        if op == "upsert" and table == "VBAP":
            raise RuntimeError("lock table overflow")
    b = build(svc, t, fault_injector=boom)
    assert b.status == "FAILED" and any("rolled back" in r for r in b.reasons) and any("removed" in r for r in b.reasons)
    assert set(svc.systems) == systems and b.system_id is None
    assert not any(s.client == "320" for s in svc.systems.values())
    assert any(e["action"] == "lean.build.failed" and e["details"]["client_removed"] for e in svc.audit.entries())
    ok = build(svc, t)                                                           # same client number can be used again
    assert ok.status == "READY"


def test_over_budget_template_is_blocked_before_anything_is_created(svc):
    t = template(svc, "functional", max_rows=50)
    systems = set(svc.systems)
    b = build(svc, t)
    assert b.status == "BLOCKED" and "exceed the template limit" in b.reasons[0] and [p["name"] for p in b.phases] == ["PLAN"]
    assert set(svc.systems) == systems


def test_missing_masking_rule_stops_the_build_and_leaves_nothing(svc):
    t = template(svc, "functional", auto_masking=False)                          # approver approved a policy without the Z-field rule
    systems = set(svc.systems)
    b = build(svc, t)
    assert b.status == "FAILED" and any("without a masking rule" in r for r in b.reasons) and set(svc.systems) == systems


# ------------------------------------------------------------------ lifecycle
def test_expiry_sweep_locks_and_decommission_rules(svc):
    b = build(svc, template(svc, "sandbox"))
    with pytest.raises(Forbidden):
        svc.lean.decommission(ALICE, b.id)
    with pytest.raises(Forbidden):
        svc.lean.decommission(COPILOT, b.id)
    p = svc.create_project(ALICE, "uses the client", svc.src_id, b.system_id)
    with pytest.raises(Conflict, match="still referenced"):
        svc.lean.decommission(CAROL, b.id)
    svc.projects.pop(p.id)
    with pytest.raises(Forbidden):
        svc.lean.sweep(TINA)
    assert svc.lean.sweep(U["svc.scheduler"], T0 + timedelta(days=10))["expired"] == []
    assert svc.lean.sweep(U["svc.scheduler"], T0 + timedelta(days=31))["expired"] == [b.id]
    assert b.status == "EXPIRED" and not svc.system(b.system_id).writable_target_allowed
    with pytest.raises(Conflict, match="expired"):
        svc.lean.set_protection(ALICE, b.system_id, locked=False)
    out = svc.lean.decommission(CAROL, b.id)
    assert b.status == "DECOMMISSIONED" and b.system_id not in svc.systems and "EQ1/300" in out["removed"]
    assert any(e["action"] == "lean.client.decommissioned" for e in svc.audit.entries()) and svc.audit.verify()["valid"]
    with pytest.raises(Conflict):
        svc.lean.decommission(CAROL, b.id)


def test_listing_shows_only_live_lean_clients(svc):
    a = build(svc, template(svc, "sandbox"), client="300")
    b = build(svc, template(svc, "sandbox"), client="301")
    assert {c["id"] for c in svc.lean.clients()} == {a.id, b.id}
    svc.lean.decommission(CAROL, a.id)
    assert [c["id"] for c in svc.lean.clients()] == [b.id]

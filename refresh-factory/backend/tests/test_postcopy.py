import copy
import io
import json
import zipfile

import pytest

from rfactory.postcopy.tasks import BY_ID, TASKS, Ctx, ordered
from rfactory.sap.adapter import SapSystem, TransientError
from rfactory.sap.synthetic import SimulatedSap
from rfactory.sap.techstate import LISTS, is_prod_ref, list_items
from rfactory.security.auth import DEMO_USERS as U, Forbidden, Principal
from rfactory.service import Conflict
from .conftest import ADMIN, ALICE, CAROL

LEAD, INTEG, SECOP = U["bastian.lead"], U["ingrid.integration"], U["sven.security"]
APPROVERS = {"basis_lead": LEAD, "integration_owner": INTEG, "security_officer": SECOP}


def ids(svc, fam="ECC"):
    return (svc.s4_src, svc.s4_tgt) if fam == "S4" else (svc.src_id, svc.tgt_id)


def approved_profile(svc, tgt):
    p = svc.postcopy.capture_profile(ALICE, tgt, "pre-copy")
    svc.postcopy.submit_profile(ALICE, p.id)
    svc.postcopy.approve_profile(CAROL, p.id)
    return p


def copied(svc, fam="ECC"):
    """Profile captured BEFORE the copy, then the simulated system copy lands production's configuration in the target."""
    src, tgt = ids(svc, fam)
    prof = approved_profile(svc, tgt)
    svc.postcopy.simulate_copy(ALICE, src, tgt)
    return src, tgt, prof


def planned(svc, fam="ECC", **kw):
    src, tgt, prof = copied(svc, fam)
    run = svc.postcopy.create_run(ALICE, {"target_id": tgt, "source_id": src, "profile_id": prof.id, **kw})
    return run, prof, tgt


def approve_all(svc, run):
    for lab in run.required_labels:
        svc.postcopy.approve_run(APPROVERS[lab], run.id, lab)


def active_prod(svc, system_id):
    return svc.postcopy.assess(system_id)["active_production_references"]


# ------------------------------------------------------------------ catalog and compatibility
def test_task_library_is_complete_ordered_and_honest():
    assert len(TASKS) == 17
    for t in TASKS:
        d = t.describe()
        assert all(d[k] for k in ("prerequisites",) ) or t.id == "PC-011"
        assert d["precheck"] and d["action"] and d["postcheck"] and d["rollback"] and d["evidence"] and d["never_reactivate_production_interfaces"]
    order = [t.id for t in ordered(None)]
    assert order.index("PC-003") < order.index("PC-001") and order.index("PC-002") < order.index("PC-001") and order.index("PC-007") < order.index("PC-011")
    assert [t.id for t in ordered(["PC-001"])][-1] == "PC-001" and {"PC-003", "PC-002"} <= {t.id for t in ordered(["PC-001"])}
    with pytest.raises(KeyError):
        ordered(["PC-999"])
    assert BY_ID["PC-011"].describe()["reversible"] and not BY_ID["PC-009"].describe()["reversible"]


def test_version_and_database_compatibility(svc):
    ecc, s4 = svc.system(svc.tgt_id), svc.system(svc.s4_tgt)
    assert not BY_ID["PC-017"].compatible(ecc)[0] and BY_ID["PC-017"].compatible(s4)[0]        # gateway aliases: S/4 only
    old = s4.model_copy(update={"release": "740"})
    ok, why = BY_ID["PC-017"].compatible(old)
    assert not ok and "SAP_BASIS" in why
    ora = svc.register_system(ADMIN, SapSystem(sid="EO1", client="200", role="QAS", db_type="ORACLE", owner="x"),
                              SimulatedSap(SapSystem(sid="EO1", client="200", role="QAS", db_type="ORACLE"), {}))
    ok, why = BY_ID["PC-011"].compatible(ora)
    assert not ok and "HANA" in why and BY_ID["PC-011"].compatible(ecc)[0]


# ------------------------------------------------------------------ simulated copy and assessment
def test_system_copy_drags_production_into_the_target(svc):
    src, tgt = ids(svc)
    assert active_prod(svc, tgt) == 0                                           # a healthy QA system
    own_params = copy.deepcopy(svc.adapters[tgt].tech.s["params"])
    svc.postcopy.simulate_copy(ALICE, src, tgt)
    a = svc.postcopy.assess(tgt)
    assert a["active_production_references"] >= 25 and a["logical_system"] == "EP1CLNT100" and not a["licence_valid"] and not a["tms_consistent"]
    assert {"FIREFIGHTER_01", "DDIC"} <= set(a["privileged_users"])
    for cat in ("rfc", "jobs", "idoc", "cloud", "integration", "printers", "schedulers", "monitoring", "certs", "smtp"):
        assert a["categories"][cat]["active_production_refs"] > 0, cat
    assert svc.adapters[tgt].tech.s["params"] == own_params                    # file-based settings stay with the target
    with pytest.raises(Forbidden):
        svc.postcopy.simulate_copy(ALICE, tgt, src)                             # never "copy" into production


# ------------------------------------------------------------------ profiles (phase 5)
def test_profile_must_be_captured_before_the_copy_and_never_from_production(svc):
    src, tgt = ids(svc)
    with pytest.raises(Forbidden):
        svc.postcopy.capture_profile(ALICE, src, "prod")                         # production is never a post-copy target
    with pytest.raises(Forbidden):
        svc.postcopy.capture_profile(U["tina.tester"], tgt, "x")
    svc.postcopy.simulate_copy(ALICE, src, tgt)
    with pytest.raises(Conflict, match="BEFORE the system copy"):
        svc.postcopy.capture_profile(ALICE, tgt, "too late")


def test_profile_with_a_hidden_production_endpoint_is_refused(svc):
    src, tgt = ids(svc)
    prod_host = svc.adapters[src].tech.s["rfc"][0]["host"]
    svc.adapters[tgt].tech.s["rfc"].append({"name": "SNEAKY", "host": prod_host, "env": "nonprod", "active": True})   # mislabelled
    with pytest.raises(Conflict, match="references production"):
        svc.postcopy.capture_profile(ALICE, tgt, "x")
    assert is_prod_ref({"host": prod_host, "env": "nonprod"}, svc.postcopy.prod_hosts())


def test_profile_governance_and_tamper_evidence(svc):
    src, tgt = ids(svc)
    p = svc.postcopy.capture_profile(ALICE, tgt, "p")
    svc.postcopy.submit_profile(ALICE, p.id)
    for who in (ALICE, U["refresh.copilot"], U["tina.tester"]):
        with pytest.raises(Forbidden):
            svc.postcopy.approve_profile(who, p.id)
    svc.postcopy.approve_profile(CAROL, p.id)
    with pytest.raises(Conflict, match="must be approved"):
        q = svc.postcopy.capture_profile(ALICE, tgt, "unapproved")
        svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": q.id})
    p.state["rfc"].append({"name": "X", "host": "evil.example", "env": "nonprod", "active": True})  # edited after approval
    with pytest.raises(Conflict, match="must be approved"):
        svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": p.id})


# ------------------------------------------------------------------ approvals
def test_run_needs_role_based_approvals_from_other_people(svc):
    run, prof, tgt = planned(svc)
    assert run.status == "AWAITING_APPROVAL" and set(run.required_labels) == {"basis_lead", "integration_owner", "security_officer"}
    with pytest.raises(Conflict, match="approvals are missing"):
        svc.postcopy.execute(ALICE, run.id)
    with pytest.raises(Forbidden):
        svc.postcopy.approve_run(INTEG, run.id, "security_officer")               # wrong role for the label
    with pytest.raises(Forbidden):
        svc.postcopy.approve_run(ALICE, run.id, "basis_lead")
    agent = Principal("copilot", "AI", ("basis_lead", "integration_owner", "security_officer"), kind="agent")
    with pytest.raises(Forbidden):
        svc.postcopy.approve_run(agent, run.id, "basis_lead")                     # agents cannot approve
    both = Principal("dual", "Dual role", ("basis", "basis_lead"))
    r2 = svc.postcopy.create_run(both, {"target_id": tgt, "profile_id": prof.id})
    with pytest.raises(Forbidden, match="separation of duties"):
        svc.postcopy.approve_run(both, r2.id, "basis_lead")                       # planner cannot approve own run
    svc.postcopy.approve_run(LEAD, run.id, "basis_lead"); svc.postcopy.approve_run(INTEG, run.id, "integration_owner")
    assert run.status == "AWAITING_APPROVAL"
    svc.postcopy.approve_run(SECOP, run.id, "security_officer")
    assert run.status == "READY"
    with pytest.raises(Forbidden):
        svc.postcopy.execute(U["tina.tester"], run.id)


# ------------------------------------------------------------------ end to end
@pytest.mark.parametrize("fam", ["ECC", "S4"])
def test_post_copy_run_removes_every_production_reference_and_restores_the_target(svc, fam):
    run, prof, tgt = planned(svc, fam)
    approve_all(svc, run)
    tech = svc.adapters[tgt].tech
    prod_before = tech.s["logsys_refs"]["EP1CLNT100" if fam == "ECC" else "S4PCLNT100"]
    run = svc.postcopy.execute(ALICE, run.id)
    assert run.status == "COMPLETED", (run.halted_reason, [t for t in run.tasks if t["status"] not in ("DONE", "ALREADY_COMPLIANT", "NOT_APPLICABLE")])
    assert run.gate["ok"] and run.pre_digest != run.post_digest
    s = tech.s
    assert active_prod(svc, tgt) == 0
    assert s["logical_system"] == prof.state["logical_system"] and list(s["logsys_refs"]) == [prof.state["logical_system"]]
    assert s["logsys_refs"][prof.state["logical_system"]] == prod_before                       # BDLS lost nothing
    users = {u["user"]: u for u in s["users"]}
    assert users["ALICE"]["locked"] and users["ALICE"]["password_reset_required"] and "SAP_ALL" not in users["FIREFIGHTER_01"]["roles"] and users["DDIC"]["locked"]
    assert not users["QA_TESTER1"]["locked"] and users["WF-BATCH"]["env"] == "nonprod"            # the target's own users are back
    live = {j["name"] for j in s["jobs"] if j["status"] in ("scheduled", "released", "active")}
    assert live == {j["name"] for j in prof.state["jobs"]} and next(j for j in s["jobs"] if j["name"] == "ZSD_EDI_OUTBOUND")["destination"] == "edi.qa.corp"
    assert next(j for j in s["jobs"] if j["name"] == "ZFI_BANK_PAYMENTS")["status"] == "cancelled"
    assert s["smtp"] == prof.state["smtp"] and svc.adapters[tgt].tech.deliver_test_mail() == "mailsink.qa.corp"
    assert s["license"]["valid"] and s["license"]["hardware_key"].endswith("QA") and s["tms"]["domain"] == "DOM_QA" and s["tms"]["consistent"]
    assert {c["subject"] for c in s["certs"]} == {c["subject"] for c in prof.state["certs"]}
    if fam == "S4":
        assert next(t for t in run.tasks if t["id"] == "PC-017")["status"] == "DONE" and all(not a["active"] for a in s["gateway"] if "prod" in a["host"])
    for cat in LISTS:
        for it in list_items(s, cat):
            if cat not in ("users",):
                assert not (is_prod_ref(it, svc.postcopy.prod_hosts()) and svc.postcopy._active(cat, it)), (cat, it)
    ev = run.evidence["PC-002"]
    assert ev["changes"] and ev["digest"] and ev["approval"]["by"] == "ingrid.integration" and ev["before"]["rfc"] != ev["after"]["rfc"]
    acts = {e["action"] for e in svc.audit.entries()}
    assert {"postcopy.profile.approved", "postcopy.run.approved", "postcopy.task.done", "postcopy.run.finished"} <= acts and svc.audit.verify()["valid"]


def test_second_run_is_idempotent_and_needs_no_approvals(svc):
    run, prof, tgt = planned(svc)
    approve_all(svc, run); svc.postcopy.execute(ALICE, run.id)
    digest = svc.adapters[tgt].tech.digest()
    r2 = svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": prof.id})
    assert r2.required_labels == [] and r2.status == "READY"
    r2 = svc.postcopy.execute(ALICE, r2.id)
    assert r2.status == "COMPLETED" and {t["status"] for t in r2.tasks} <= {"ALREADY_COMPLIANT", "NOT_APPLICABLE"}
    assert svc.adapters[tgt].tech.digest() == digest


def test_hidden_production_endpoint_is_neutralised_even_if_mislabelled(svc):
    src, tgt, prof = copied(svc)
    prod_host = svc.adapters[src].tech.s["rfc"][0]["host"]
    svc.adapters[tgt].tech.s["rfc"].append({"name": "DISGUISED", "host": prod_host, "env": "nonprod", "active": True})
    run = svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": prof.id}); approve_all(svc, run)
    assert svc.postcopy.execute(ALICE, run.id).status == "COMPLETED"
    d = next(i for i in svc.adapters[tgt].tech.s["rfc"] if i["name"] == "DISGUISED")
    assert d["active"] is False and d["neutralized"]


def test_delete_mode_removes_instead_of_deactivating(svc):
    run, prof, tgt = planned(svc, mode="delete"); approve_all(svc, run)
    assert svc.postcopy.execute(ALICE, run.id).status == "COMPLETED"
    names = {i["name"] for i in svc.adapters[tgt].tech.s["rfc"]}
    assert names == {i["name"] for i in prof.state["rfc"]}
    with pytest.raises(Conflict):
        svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": prof.id, "mode": "wipe"})


# ------------------------------------------------------------------ failure, retry, rollback
def test_postcheck_failure_restores_the_task_and_halts_then_resumes(svc):
    run, prof, tgt = planned(svc); approve_all(svc, run)
    tech = svc.adapters[tgt].tech

    def sabotage(task_id, stage, ctx=None):
        if task_id == "PC-002" and stage == "after_apply":
            ctx.tech.s["rfc"].append({"name": "REACTIVATED", "host": "bwp.prod.corp", "env": "prod", "active": True})   # a production link comes back
    before_rfc = copy.deepcopy(tech.s["rfc"])
    run = svc.postcopy.execute(ALICE, run.id, fault_hook=sabotage)
    t = next(x for x in run.tasks if x["id"] == "PC-002")
    assert run.status == "HALTED" and t["status"] == "POSTCHECK_FAILED" and "REACTIVATED" in t["reason"] and "restored" in t["reason"]
    assert tech.s["rfc"] == before_rfc and not any(i["name"] == "REACTIVATED" for i in tech.s["rfc"])   # task state restored exactly
    assert {x["id"]: x["status"] for x in run.tasks}["PC-003"] == "DONE" and {x["id"]: x["status"] for x in run.tasks}["PC-005"] == "PENDING"
    run = svc.postcopy.resume(ALICE, run.id)
    assert run.status == "COMPLETED" and active_prod(svc, tgt) == 0 and run.attempts == 2


def test_transient_errors_are_retried_permanent_errors_restore_and_halt(svc):
    run, prof, tgt = planned(svc); approve_all(svc, run)
    n = {"c": 0}

    def flaky(task_id, stage, ctx=None):
        if task_id == "PC-004" and stage == "before_apply":
            n["c"] += 1
            if n["c"] <= 2:
                raise TransientError("RFC timeout")
    run = svc.postcopy.execute(ALICE, run.id, fault_hook=flaky)
    assert run.status == "COMPLETED" and sum(1 for e in run.events if e["level"] == "warn") == 2
    run2, prof2, tgt2 = planned(svc, "S4"); approve_all(svc, run2)
    snap = copy.deepcopy(svc.adapters[tgt2].tech.s["jobs"])

    def dead(task_id, stage, ctx=None):
        if task_id == "PC-003" and stage == "before_apply":
            raise RuntimeError("enqueue lock")
    run2 = svc.postcopy.execute(ALICE, run2.id, fault_hook=dead)
    assert run2.status == "HALTED" and run2.tasks[0]["status"] == "FAILED" and svc.adapters[tgt2].tech.s["jobs"] == snap


def test_full_rollback_restores_the_exact_pre_run_state(svc):
    run, prof, tgt = planned(svc); approve_all(svc, run)
    tech = svc.adapters[tgt].tech
    before = tech.digest()
    run = svc.postcopy.execute(ALICE, run.id)
    assert run.status == "COMPLETED" and tech.digest() != before
    for who in (ALICE, U["refresh.copilot"], INTEG):
        with pytest.raises(Forbidden):
            svc.postcopy.rollback(who, run.id)
    run = svc.postcopy.rollback(LEAD, run.id)
    assert run.status == "ROLLED_BACK" and tech.digest() == before and run.pre_digest == tech.digest()
    assert not svc.postcopy.gate(tgt, prof.id)["ok"]                              # the target is unsafe again, and the gate says so


def test_bdls_waits_for_its_prerequisites_and_hana_check_never_changes_hana(svc):
    src, tgt, prof = copied(svc)
    t, c = BY_ID["PC-001"], Ctx(svc.adapters[tgt].tech, prof.state, svc.postcopy.prod_hosts())
    assert "PC-003" in t.blocking(c)[0]
    c.completed = {"PC-003", "PC-002"}
    assert t.blocking(c) == []
    svc.adapters[tgt].tech.s["hana"]["backup_catalog_ok"] = False
    hana_before = copy.deepcopy(svc.adapters[tgt].tech.s["hana"])
    run = svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": prof.id}); approve_all(svc, run)
    run = svc.postcopy.execute(ALICE, run.id)
    h = next(x for x in run.tasks if x["id"] == "PC-011")
    assert run.status == "HALTED" and h["status"] == "POSTCHECK_FAILED" and "backup catalog" in h["reason"]
    assert svc.adapters[tgt].tech.s["hana"] == hana_before                        # verification only: never modified


# ------------------------------------------------------------------ guards and evidence
def test_production_and_locked_targets_are_refused_and_production_is_never_touched(svc):
    src, tgt = ids(svc)
    prod_digest = svc.adapters[src].tech.digest()
    prof = approved_profile(svc, tgt)
    svc.postcopy.simulate_copy(ALICE, src, tgt)
    with pytest.raises(Forbidden):
        svc.postcopy.create_run(ALICE, {"target_id": src, "profile_id": prof.id})
    run = svc.postcopy.create_run(ALICE, {"target_id": tgt, "profile_id": prof.id}); approve_all(svc, run)
    svc.system(tgt).writable_target_allowed = False
    with pytest.raises(Forbidden, match="locked"):
        svc.postcopy.execute(ALICE, run.id)
    svc.system(tgt).writable_target_allowed = True
    svc.postcopy.execute(ALICE, run.id)
    assert svc.adapters[src].tech.digest() == prod_digest


def test_evidence_package_is_complete_hashed_and_free_of_credentials(svc):
    run, prof, tgt = planned(svc); approve_all(svc, run); svc.postcopy.execute(ALICE, run.id)
    z = zipfile.ZipFile(io.BytesIO(svc.postcopy.evidence_package(run.id)))
    names = set(z.namelist())
    assert {"run.json", "gate.json", "profile.json", "audit.json", "SHA256SUMS.json", "tasks/PC-009.json", "tasks/PC-002.json"} <= names
    import hashlib
    for n, h in json.loads(z.read("SHA256SUMS.json"))["files"].items():
        assert hashlib.sha256(z.read(n)).hexdigest() == h
    blob = " ".join(z.read(n).decode() for n in names).lower()
    assert "password\":" not in blob and "secret" not in blob and "credential" not in blob.replace("no credentials", "")
    assert json.loads(z.read("gate.json"))["ok"]

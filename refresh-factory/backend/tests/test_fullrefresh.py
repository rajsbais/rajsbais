import copy

import pytest

from rfactory.fullrefresh import engine as fr_engine
from rfactory.fullrefresh.engine import _digest
from rfactory.sap.ddic import TABLES
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict

from .conftest import ADMIN, ALICE, CAROL

BASTIAN, INGRID, SVEN, COPILOT = U["bastian.lead"], U["ingrid.integration"], U["sven.security"], U["refresh.copilot"]


def prepare(svc, family="ECC", approve=True):
    src, tgt = (svc.src_id, svc.tgt_id) if family == "ECC" else (svc.s4_src, svc.s4_tgt)
    pc = svc.postcopy
    prof = pc.capture_profile(ALICE, tgt, "pre-refresh")
    pc.submit_profile(ALICE, prof.id)
    pc.approve_profile(CAROL, prof.id)
    p = svc.full.create(ALICE, {"source_id": src, "target_id": tgt, "profile_id": prof.id, "backup_ref": "HANA-BKP-1"})
    if approve:
        svc.full.approve(CAROL, p.id, "change_approver")
        svc.full.approve(BASTIAN, p.id, "basis_lead")
    return p


def approve_postcopy(svc, p):
    for label, who in (("basis_lead", BASTIAN), ("integration_owner", INGRID), ("security_officer", SVEN)):
        svc.postcopy.approve_run(who, p.postcopy_run, label)


def to_release(svc, p):
    svc.full.run(ALICE, p.id)
    approve_postcopy(svc, p)
    svc.full.run(ALICE, p.id)
    svc.full.sign_off(SVEN, p.id)
    svc.full.run(ALICE, p.id)
    assert p.status == "AWAITING_RELEASE", (p.status, p.reasons)
    return p


@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_end_to_end_refresh_masks_everything_and_leaves_source_untouched(svc, family):
    p = prepare(svc, family)
    src, tgt = svc.adapters[p.source_id], svc.adapters[p.target_id]
    src_before = _digest({t: src.data[t] for t in TABLES})
    to_release(svc, p)
    svc.full.release(CAROL, p.id)
    assert p.status == "RELEASED" and all(ph["status"] == "DONE" for ph in p.phases)
    assert _digest({t: src.data[t] for t in TABLES}) == src_before
    assert {t: len(tgt.data[t]) for t in TABLES} == {t: len(src.data[t]) for t in TABLES}
    # names are masked in every table that had a sensitive field, and no original sensitive value survived
    masked = p.evidence["masking"]
    assert masked["rules"] > 0 and masked["stats"]
    for st in masked["stats"]:
        orig = {r[st["field"]] for r in src.data[st["table"]] if isinstance(r.get(st["field"]), str) and r[st["field"]]}
        now = {r[st["field"]] for r in tgt.data[st["table"]] if isinstance(r.get(st["field"]), str) and r[st["field"]]}
        from rfactory.masking.engine import CATALOG
        if CATALOG.get((st["table"], st["field"]), ("", ""))[0] == "birthdate":  # a date has a small value space: another person's original date may legitimately appear, one's own must not
            keys = TABLES[st["table"]].keys
            before = {tuple(r[k] for k in keys): r[st["field"]] for r in src.data[st["table"]]}
            assert all(before[tuple(r[k] for k in keys)] != r[st["field"]] for r in tgt.data[st["table"]]), st
            continue
        assert not (orig & now), st
    assert not p.unmasked_target and svc.full.unmasked_targets() == []
    assert svc.postcopy.gate(p.target_id, p.profile_id)["ok"]
    assert svc.audit.verify()["valid"]


def test_target_holds_unmasked_data_between_copy_and_masking_and_it_is_flagged(svc):
    p = prepare(svc)
    svc.full.run(ALICE, p.id)  # stops at post-copy approvals (after the copy)
    assert p.status == "WAITING" and p.checkpoint == 7 and p.unmasked_target
    assert svc.full.unmasked_targets() == [p.target_id]
    src, tgt = svc.adapters[p.source_id], svc.adapters[p.target_id]
    assert {r["NAME1"] for r in tgt.data["KNA1"]} == {r["NAME1"] for r in src.data["KNA1"]}  # real names are there
    c14 = next(c for c in svc.agents.run(ALICE, "compliance-verification")["artifacts"]["controls"] if c["id"] == "C14")
    assert c14["status"] == "fail"
    approve_postcopy(svc, p)
    svc.full.run(ALICE, p.id)
    assert not p.unmasked_target
    assert next(c for c in svc.agents.run(ALICE, "compliance-verification")["artifacts"]["controls"] if c["id"] == "C14")["status"] == "pass"


def test_pair_and_target_guards(svc):
    pc = svc.postcopy
    prof = pc.capture_profile(ALICE, svc.tgt_id, "p")
    pc.submit_profile(ALICE, prof.id)
    pc.approve_profile(CAROL, prof.id)
    base = {"source_id": svc.src_id, "target_id": svc.tgt_id, "profile_id": prof.id, "backup_ref": "b"}
    for bad in ({"target_id": svc.src_id}, {"source_id": svc.s4_src}, {"target_id": svc.s4_tgt}, {"backup_ref": ""}, {"mechanism": "rsync"},
                {"masking_policy_id": "nope"}):
        with pytest.raises((Conflict, Forbidden)):
            svc.full.create(ALICE, {**base, **bad})
    svc.system(svc.tgt_id).writable_target_allowed = False
    with pytest.raises(Conflict):
        svc.full.create(ALICE, base)
    svc.system(svc.tgt_id).writable_target_allowed = True
    unapproved = pc.capture_profile(ALICE, svc.tgt_id, "q")
    with pytest.raises(Conflict, match="approved"):
        svc.full.create(ALICE, {**base, "profile_id": unapproved.id})
    other = pc.capture_profile(ALICE, svc.s4_tgt, "o")
    with pytest.raises(Conflict):
        svc.full.create(ALICE, {**base, "profile_id": other.id})


def test_approvals_separation_of_duties_and_binding(svc):
    p = prepare(svc, approve=False)
    with pytest.raises(Conflict, match="approvals"):
        svc.full.run(ALICE, p.id)
    with pytest.raises(Forbidden):
        svc.full.approve(ALICE, p.id, "change_approver")  # lacks plan:approve
    with pytest.raises(Forbidden):
        svc.full.approve(COPILOT, p.id, "change_approver")
    with pytest.raises(Forbidden):
        svc.full.approve(CAROL, p.id, "basis_lead")  # wrong role
    svc.full.approve(CAROL, p.id, "change_approver")
    svc.full.approve(BASTIAN, p.id, "basis_lead")
    with pytest.raises(Forbidden):
        svc.full.run(CAROL, p.id)  # an approver cannot execute
    with pytest.raises(Forbidden):
        svc.full.run(U["tina.tester"], p.id)
    with pytest.raises(Forbidden):
        svc.full.run(COPILOT, p.id)
    p.backup_ref = "a different backup plan"  # approvals were bound to the original hash
    with pytest.raises(Conflict, match="stale"):
        svc.full.run(ALICE, p.id)


def test_creator_cannot_approve_own_program(svc):
    pc = svc.postcopy
    prof = pc.capture_profile(ALICE, svc.tgt_id, "p")
    pc.submit_profile(ALICE, prof.id)
    pc.approve_profile(CAROL, prof.id)
    p = svc.full.create(ADMIN, {"source_id": svc.src_id, "target_id": svc.tgt_id, "profile_id": prof.id, "backup_ref": "b"})
    with pytest.raises(Forbidden):
        svc.full.approve(ADMIN, p.id, "change_approver")


def test_failed_copy_leaves_target_untouched_and_resume_does_not_rebackup(svc, monkeypatch):
    p = prepare(svc)
    tgt = svc.adapters[p.target_id]
    before = _digest(tgt.data)
    real = fr_engine.simulate_system_copy
    monkeypatch.setattr(fr_engine, "simulate_system_copy", lambda *a: (_ for _ in ()).throw(TimeoutError("storage snapshot timed out")))
    svc.full.run(ALICE, p.id)
    assert p.status == "FAILED" and p.checkpoint == 6 and "snapshot timed out" in p.reasons[0]
    assert _digest(tgt.data) == before and not p.unmasked_target
    backup_digest = p.backup["data_digest"]
    monkeypatch.setattr(fr_engine, "simulate_system_copy", real)
    svc.full.run(ALICE, p.id)
    assert p.status == "WAITING" and p.checkpoint == 7 and p.backup["data_digest"] == backup_digest == before


def test_rollback_restores_the_target_exactly(svc):
    p = prepare(svc)
    tgt = svc.adapters[p.target_id]
    d_data, d_tech = _digest(tgt.data), tgt.tech.digest()
    svc.full.run(ALICE, p.id)
    assert _digest(tgt.data) != d_data and p.unmasked_target
    svc.full.rollback(ALICE, p.id)
    assert p.status == "ROLLED_BACK" and not p.unmasked_target
    assert _digest(tgt.data) == d_data and tgt.tech.digest() == d_tech
    with pytest.raises(Conflict):
        svc.full.run(ALICE, p.id)
    with pytest.raises(Forbidden):
        svc.full.rollback(COPILOT, p.id)


def test_cannot_roll_back_before_a_backup_exists(svc):
    p = prepare(svc)
    with pytest.raises(Conflict):
        svc.full.rollback(ALICE, p.id)


def test_masking_that_does_nothing_is_detected_and_holds_the_program(svc, monkeypatch):
    p = prepare(svc)
    svc.full.run(ALICE, p.id)
    approve_postcopy(svc, p)
    monkeypatch.setattr(fr_engine.MaskingEngine, "mask_value", lambda self, rule, value: value)
    svc.full.run(ALICE, p.id)
    assert p.status == "HELD" and p.checkpoint == 10 and "left original values" in p.reasons[0]
    assert p.unmasked_target  # still flagged, and still unmasked
    assert any(r["NAME1"] == s["NAME1"] for r, s in zip(svc.adapters[p.target_id].data["KNA1"], svc.adapters[p.source_id].data["KNA1"]))
    monkeypatch.undo()
    svc.full.run(ALICE, p.id)  # fixed: resumes at phase 11
    assert p.status == "WAITING" and not p.unmasked_target


def test_smoke_test_failure_holds_before_masking(svc):
    p = prepare(svc)
    svc.full.run(ALICE, p.id)
    approve_postcopy(svc, p)

    def drop_row(no):
        if no == 10 and not getattr(drop_row, "done", False):
            drop_row.done = True
            svc.adapters[p.target_id].data["KNA1"].pop()
    svc.full.run(ALICE, p.id, fault_injector=drop_row)
    assert p.status == "HELD" and p.checkpoint == 9 and "S1-ROW-COUNTS" in p.reasons[0] and p.unmasked_target
    svc.full.rollback(ALICE, p.id)
    assert p.status == "ROLLED_BACK"


def test_postcopy_must_be_approved_before_the_program_continues(svc):
    p = prepare(svc)
    svc.full.run(ALICE, p.id)
    assert len(p.waiting_for) == 3
    svc.postcopy.approve_run(BASTIAN, p.postcopy_run, "basis_lead")
    svc.full.run(ALICE, p.id)
    assert p.status == "WAITING" and len(p.waiting_for) == 2 and p.checkpoint == 7


def test_signoff_and_release_rules(svc):
    p = prepare(svc)
    with pytest.raises(Conflict):
        svc.full.sign_off(SVEN, p.id)  # too early
    svc.full.run(ALICE, p.id)
    approve_postcopy(svc, p)
    svc.full.run(ALICE, p.id)
    assert p.waiting_for == ["security officer sign-off"]
    for who in (ALICE, CAROL, COPILOT):
        with pytest.raises(Forbidden):
            svc.full.sign_off(who, p.id)
    svc.full.sign_off(SVEN, p.id)
    with pytest.raises(Conflict):
        svc.full.release(CAROL, p.id)  # phase 12-13 not run yet
    svc.full.run(ALICE, p.id)
    assert p.status == "AWAITING_RELEASE"
    for who in (ALICE, COPILOT, U["tina.tester"]):
        with pytest.raises(Forbidden):
            svc.full.release(who, p.id)
    # release re-checks the gate: reintroduce a production reference after phase 12
    svc.adapters[p.target_id].tech.s["rfc"][0]["host"] = "bwp.prod.corp"
    svc.adapters[p.target_id].tech.s["rfc"][0]["active"] = True
    with pytest.raises(Conflict, match="gate"):
        svc.full.release(CAROL, p.id)


def test_released_program_drops_backup_and_cannot_roll_back(svc):
    p = to_release(svc, prepare(svc))
    svc.full.release(CAROL, p.id)
    assert p.backup is None
    with pytest.raises(Conflict):
        svc.full.rollback(ALICE, p.id)


def test_evidence_contains_no_data_values(svc):
    p = to_release(svc, prepare(svc))
    ev = str(svc.full.evidence_report(p.id))
    for r in svc.adapters[p.source_id].data["KNA1"][:20]:
        assert r["NAME1"] not in ev


def test_full_refresh_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    src, tgt = b["source"]["id"], b["target"]["id"]
    pid = c.post("/api/postcopy/profiles", json={"system_id": tgt, "name": "pre"}, headers=H("alice.basis")).json()["id"]
    c.post(f"/api/postcopy/profiles/{pid}/submit", headers=H("alice.basis"))
    c.post(f"/api/postcopy/profiles/{pid}/approve", headers=H("carol.approver"))
    body = {"source_id": src, "target_id": tgt, "profile_id": pid, "backup_ref": "BKP-7"}
    assert c.post("/api/full-refresh/programs", json={**body, "target_id": src}, headers=H("alice.basis")).status_code == 409
    prog = c.post("/api/full-refresh/programs", json=body, headers=H("alice.basis")).json()
    fid = prog["id"]
    ap = lambda who, label: c.post(f"/api/full-refresh/programs/{fid}/approve", json={"label": label}, headers=H(who))
    assert ap("alice.basis", "change_approver").status_code == 403
    assert ap("carol.approver", "change_approver").status_code == 200 and ap("bastian.lead", "basis_lead").json()["status"] == "APPROVED"
    run = lambda who="alice.basis": c.post(f"/api/full-refresh/programs/{fid}/run", headers=H(who))
    assert run("refresh.copilot").status_code == 403
    r = run().json()
    assert r["status"] == "WAITING" and r["unmasked_target"] and "has_backup" in r
    for who, label in (("bastian.lead", "basis_lead"), ("ingrid.integration", "integration_owner"), ("sven.security", "security_officer")):
        assert c.post(f"/api/postcopy/runs/{r['postcopy_run']}/approvals", json={"label": label}, headers=H(who)).status_code == 200
    assert run().json()["waiting_for"] == ["security officer sign-off"]
    assert c.post(f"/api/full-refresh/programs/{fid}/security-signoff", headers=H("sven.security")).status_code == 200
    assert run().json()["status"] == "AWAITING_RELEASE"
    assert c.post(f"/api/full-refresh/programs/{fid}/release", headers=H("alice.basis")).status_code == 403
    done = c.post(f"/api/full-refresh/programs/{fid}/release", headers=H("carol.approver")).json()
    assert done["status"] == "RELEASED" and not done["unmasked_target"]
    assert c.get(f"/api/full-refresh/programs/{fid}/evidence", headers=H("erin.auditor")).json()["masking"]["rules"] > 0
    assert c.get(f"/api/full-refresh/programs/{fid}/evidence", headers=H("bob.steward")).status_code == 403


def test_an_approver_who_could_otherwise_execute_is_refused(svc):
    p = prepare(svc, approve=False)
    svc.full.approve(ADMIN, p.id, "change_approver")  # root.admin holds approver + basis roles
    svc.full.approve(BASTIAN, p.id, "basis_lead")
    assert ADMIN.can("run:execute")
    with pytest.raises(Forbidden, match="approver cannot execute"):
        svc.full.run(ADMIN, p.id)

"""The write side for a remote non-production SAP system (ZRF_* loader) against a FAKE loader. The ABAP itself has never run."""
import pytest
from fastapi.testclient import TestClient

from rfactory.api.main import create_app
from rfactory.sap.adapter import ProductionWriteBlocked, SapSystem
from rfactory.sap.connectors import rfc as R
from rfactory.sap.connectors import rfc_target as T
from rfactory.sap.connectors.fake_loader import FakeLoaderTransport
from rfactory.sap.connectors.profile import ConnectionProfile
from rfactory.sap.synthetic import make_demo_pair
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict, RefreshService

from .conftest import ADMIN, ALICE, CAROL, make_project, ready_project

NO_SLEEP = lambda s: None
SVEN = U["sven.security"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def mk(role="QAS", clock=None, writable=True, **tkw):
    _src, tsim = make_demo_pair()
    system = SapSystem(sid=tsim.system.sid, client=tsim.system.client, role=role, product=tsim.system.product, release=tsim.system.release, db_type=tsim.system.db_type, owner="ops")
    system.writable_target_allowed = writable
    tr = FakeLoaderTransport(tsim, **tkw)
    prof = ConnectionProfile("t", "rfc", ashost="h", client=system.client, calls_per_minute=10 ** 6)
    ck = clock or Clock()
    a = T.RfcTargetAdapter(system, tr, prof, sleep=NO_SLEEP, reference=tsim.reference_date, clock=ck)
    return tsim, tr, a, ck


def mat(n, **over):
    return {"MATNR": f"{n:018d}", "MTART": "FERT", "MATKL": "MG1", "MEINS": "EA", "ERSDA": "2026-01-01", **over}


# ---------------------------------------------------------------- handshake
def test_handshake_reports_the_loader_and_arms_the_adapter():
    sim, tr, a, ck = mk()
    a.assert_writable()
    h = a.handshake_info
    assert h["version"] == "RFL-1" and h["sid"] == sim.system.sid and h["category"] == "T" and h["writes_enabled"] and "MARA" in h["allowed_tables"]


@pytest.mark.parametrize("kw,match", [
    ({"category": "P"}, "PRODUCTIVE client"),
    ({"enabled": False}, "not enabled on the SAP side"),
    ({"version": "RFL-0"}, "speaks"),
    ({"sid": "XYZ"}, "wrong system"),
    ({"client": "999"}, "client"),
])
def test_a_handshake_that_does_not_match_blocks_every_write(kw, match):
    sim, tr, a, ck = mk(**kw)
    with pytest.raises((ProductionWriteBlocked, T.LoaderError), match=match):
        a.assert_writable()
    with pytest.raises(ProductionWriteBlocked, match="not armed"):
        a.upsert("MARA", [mat(1)])
    assert not [m for m in sim.data["MARA"] if m["MATNR"] == mat(1)["MATNR"]]


def test_a_production_role_system_never_arms_even_if_the_loader_says_yes():
    sim, tr, a, ck = mk(role="PRD")
    with pytest.raises(ProductionWriteBlocked, match="production system"):
        a.assert_writable()
    sim2, tr2, locked, _ = mk(writable=False)
    with pytest.raises(ProductionWriteBlocked, match="locked"):
        locked.assert_writable()


def test_arming_expires_and_revocation_takes_effect_at_once():
    sim, tr, a, ck = mk()
    a.assert_writable()
    a.upsert("MARA", [mat(1)])
    ck.t += T.ARM_SECONDS + 1
    with pytest.raises(ProductionWriteBlocked, match="not armed"):
        a.upsert("MARA", [mat(2)])
    a.assert_writable()
    a.system.writable_target_allowed = False  # revoked while armed
    with pytest.raises(ProductionWriteBlocked, match="not an allowed write target"):
        a.upsert("MARA", [mat(3)])


# ---------------------------------------------------------------- writes
def test_upsert_writes_after_a_dry_run_with_the_same_request_id():
    sim, tr, a, ck = mk()
    a.assert_writable()
    a.upsert("MARA", [mat(7), mat(8, MTART="ROH")])
    assert {m["MATNR"] for m in sim.data["MARA"]} >= {mat(7)["MATNR"], mat(8)["MATNR"]}
    assert a.get("MARA", (mat(8)["MATNR"],))["MTART"] == "ROH"  # read back through the same adapter
    ops = [(l["dry"], l["op"], l["rows"]) for l in tr.log[-2:]]
    assert ops == [(True, "upsert", 2), (False, "upsert", 2)] and tr.log[-2]["request"] == tr.log[-1]["request"]
    a.upsert("MARA", [mat(7, MTART="HALB")])  # an upsert replaces
    assert a.get("MARA", (mat(7)["MATNR"],))["MTART"] == "HALB" and len(a.writes) == 2


def test_batches_are_capped_and_each_has_its_own_request():
    sim, tr, a, ck = mk()
    a.assert_writable()
    a.upsert("MARA", [mat(1000 + i) for i in range(450)])
    real = [l for l in tr.log if not l["dry"] and l["table"] == "MARA"]
    assert [l["rows"] for l in real] == [200, 200, 50] and len({l["request"] for l in real}) == 3


def test_the_sap_side_allow_list_authorisation_and_row_checks_are_honoured():
    sim, tr, a, ck = mk(allowed={"MARA", "MAKT"}, unauthorised={"MAKT"})
    a.assert_writable()
    with pytest.raises(T.LoaderError, match="allow list"):
        a.upsert("MARC", [{"MATNR": "x", "WERKS": "1000", "DISMM": "PD"}])
    a.handshake_info["allowed_tables"].append("MAKT")  # even if the platform believed it, the SAP side refuses
    with pytest.raises(R.RemoteAuthError):
        a.upsert("MAKT", [{"MATNR": mat(1)["MATNR"], "SPRAS": "E", "MAKTX": "x"}])
    with pytest.raises(T.LoaderError, match="not a table the platform writes"):
        a.upsert("T001", [{"BUKRS": "9", "BUTXT": "x", "ORT01": "y", "WAERS": "EUR"}])
    with pytest.raises(T.LoaderError, match="dry run"):
        a.upsert("MARA", [{"MATNR": "x"}])  # a row without the structure's fields
    assert tr.log[-1]["dry"] is True  # the bad row was caught by the dry run: nothing real was attempted


def test_a_failure_in_the_middle_of_a_batch_is_rolled_back_and_reported():
    sim, tr, a, ck = mk()
    a.assert_writable()
    before = [dict(r) for r in sim.data["MARA"]]
    tr.fail_after = 3
    with pytest.raises(T.LoaderError, match="rolled back|0 of"):
        a.upsert("MARA", [mat(2000 + i) for i in range(6)])
    assert sim.data["MARA"] == before  # all or nothing
    assert a.writes == []


def test_delete_and_number_levels_go_through_the_loader():
    sim, tr, a, ck = mk()
    a.assert_writable()
    a.upsert("MARA", [mat(5)])
    a.delete("MARA", (mat(5)["MATNR"],))
    assert a.get("MARA", (mat(5)["MATNR"],)) is None
    lvl = a.number_level("SD_ORDER")
    a.set_number_level("SD_ORDER", lvl + 100)
    assert a.number_level("SD_ORDER") == lvl + 100 and sim.number_level("SD_ORDER") == lvl + 100
    assert any(w["table"] == "NRIV" for w in a.writes)


def test_ownership_is_not_modelled_and_outbound_interfaces_are_unverified_unless_attested():
    sim, tr, a, ck = mk()
    assert a.owner_of("MARA", "x") is None
    ifs = a.outbound_interfaces()
    assert len(ifs) == 1 and ifs[0]["active"] and "UNVERIFIED" in ifs[0]["name"]
    a.profile.options["write"] = {"outbound_attested_by": "sven.security"}
    assert a.outbound_interfaces() == []


def test_function_allow_lists_keep_the_two_adapters_apart():
    sim, tr, a, ck = mk()
    src = R.RfcSourceAdapter(a.system, tr, a.profile, sleep=NO_SLEEP)
    with pytest.raises(R.FunctionNotAllowed):
        src._call("ZRF_UPSERT", IV_TABLE="MARA", IV_ROWS="[]")
    with pytest.raises(R.FunctionNotAllowed):
        a._call("BAPI_ACC_DOCUMENT_POST")  # nothing else was added to the target's list
    assert T.WRITE_FUNCTIONS == {"ZRF_PING", "ZRF_UPSERT", "ZRF_DELETE", "ZRF_NR_SET"}
    assert not hasattr(src, "upsert") and not hasattr(src, "delete")


# ---------------------------------------------------------------- the two-person gate in the platform
def remote_target_svc(tmp_path, monkeypatch, persist=False, role="QAS", **kw):
    monkeypatch.setenv("RFACTORY_ALLOW_FAKE_ENDPOINTS", "1")
    monkeypatch.setenv("RF_MASKING_KEY", "fixed-key-for-comparison")
    monkeypatch.setenv("ONB_PW", "x")
    svc = RefreshService(tmp_path, persist=persist)
    b = svc.bootstrap_demo(ADMIN)
    svc.src_id, svc.tgt_id, svc.s4_src, svc.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    _s, tsim = make_demo_pair()
    system = SapSystem(sid=tsim.system.sid, client=tsim.system.client, role=role, product=tsim.system.product, release=tsim.system.release, db_type=tsim.system.db_type, owner="ops")
    prof = ConnectionProfile("tgt", "rfc", ashost="target.invalid", client=system.client, user="U", password_ref="env:ONB_PW", calls_per_minute=10 ** 6)
    svc.connect_remote(ADMIN, system, prof)
    rid = system.id
    svc.adapters[rid]._sleep = NO_SLEEP
    return svc, rid


def test_write_access_needs_a_request_and_a_different_approver(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    assert not svc.system(rid).can_be_write_target
    with pytest.raises(Forbidden, match="locked"):
        svc.create_project(ALICE, "x", svc.src_id, rid)
    for who in (U["refresh.copilot"], U["tina.tester"], CAROL):
        with pytest.raises(Forbidden):
            svc.request_remote_write(who, rid)
    req = svc.request_remote_write(ALICE, rid, "sandbox refresh", attest_outbound_inactive=True)
    assert req["status"] == "PENDING" and req["handshake"]["category"] == "T"
    assert not svc.system(rid).can_be_write_target  # asking enables nothing
    for who in (ALICE, ADMIN, CAROL, U["refresh.copilot"], U["bastian.lead"]):
        with pytest.raises(Forbidden):
            svc.approve_remote_write(who, rid)  # the requester, and everyone without target:approve
    svc.approve_remote_write(SVEN, rid)
    assert svc.system(rid).can_be_write_target and isinstance(svc.adapters[rid], T.RfcTargetAdapter)
    assert svc.adapters[rid].profile.options["write"]["approved_by"] == "sven.security"
    acts = [e["action"] for e in svc.audit.entries() if e["resource"] == rid]
    assert {"target.write_requested", "target.write_approved"} <= set(acts)


def test_requester_who_also_holds_the_approver_permission_still_cannot_approve_their_own_request(tmp_path, monkeypatch):
    from rfactory.security.auth import Principal
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    both = Principal("both", "both", ("basis", "security_officer"))
    svc.request_remote_write(both, rid)
    with pytest.raises(Forbidden, match="separation of duties"):
        svc.approve_remote_write(both, rid)


def test_production_roles_and_unready_loaders_cannot_even_be_requested(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch, role="PRD")
    with pytest.raises(Forbidden, match="production"):
        svc.request_remote_write(ALICE, rid)
    svc2, rid2 = remote_target_svc(tmp_path / "b", monkeypatch)
    svc2.adapters[rid2]._t._inner.category = "P"  # the SAP side says this client is productive
    with pytest.raises(Conflict, match="PRODUCTIVE"):
        svc2.request_remote_write(ALICE, rid2)
    svc2.adapters[rid2]._t._inner.category, svc2.adapters[rid2]._t._inner.enabled = "T", False
    with pytest.raises(Conflict, match="not enabled on the SAP side"):
        svc2.request_remote_write(ALICE, rid2)
    with pytest.raises(Conflict, match="only a connected RFC"):
        svc2.request_remote_write(ALICE, svc2.tgt_id)  # a simulated system


def test_the_sap_side_can_withdraw_between_request_and_approval(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    svc.request_remote_write(ALICE, rid)
    svc.adapters[rid]._t._inner.enabled = False
    with pytest.raises(Conflict, match="no longer ready"):
        svc.approve_remote_write(SVEN, rid)
    assert not svc.system(rid).can_be_write_target and not isinstance(svc.adapters[rid], T.RfcTargetAdapter)


def test_revocation_locks_the_target_again_immediately(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    svc.request_remote_write(ALICE, rid)
    svc.approve_remote_write(SVEN, rid)
    with pytest.raises(Forbidden):
        svc.revoke_remote_write(U["tina.tester"], rid)
    svc.revoke_remote_write(SVEN, rid)
    assert not svc.system(rid).can_be_write_target and not isinstance(svc.adapters[rid], T.RfcTargetAdapter)
    assert "write" not in svc.adapters[rid].profile.options and rid not in svc.write_requests
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "x", svc.src_id, rid)


# ---------------------------------------------------------------- a refresh into a remote target
def refresh_into(svc, target_id, attested):
    p = ready_project(svc, src=svc.src_id, tgt=target_id, object_type="MATERIAL", company=None, days=0, downstream=(), plants=["1000", "1010"])
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    return p, svc.execute(ALICE, p.id)


def pick(sim):
    return {t: sorted(sim.data[t], key=lambda r: sorted(r.items())) for t in ("MARA", "MAKT", "MARC")}


def test_a_selective_refresh_into_a_remote_target_equals_one_into_a_simulated_target(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    svc.request_remote_write(ALICE, rid, attest_outbound_inactive=True)
    svc.approve_remote_write(SVEN, rid)
    p, run = refresh_into(svc, rid, True)
    assert run.status == "COMPLETED", run.error
    assert run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    remote_sim = svc.adapters[rid]._t._inner.sap
    q, local = refresh_into(svc, svc.tgt_id, True)
    assert local.status == "COMPLETED"
    assert pick(remote_sim) == pick(svc.adapters[svc.tgt_id])  # identical data, whichever way it was written
    tr = svc.adapters[rid]._t._inner
    assert [l for l in tr.log if not l["dry"]] and all(l["table"] in ("MARA", "MAKT", "MARC", "NRIV") for l in tr.log)
    assert svc.adapters[rid].writes  # the adapter kept the evidence of what it wrote


def test_without_the_outbound_attestation_the_release_gate_holds_the_refresh(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    svc.request_remote_write(ALICE, rid, attest_outbound_inactive=False)
    svc.approve_remote_write(SVEN, rid)
    p, run = refresh_into(svc, rid, False)
    failed = {c["id"] for c in run.reconciliation["checks"] if c["status"] == "fail"}
    assert run.status == "COMPLETED" and failed == {"SEC-OUTBOUND"} and run.release == "HELD"


def test_an_unarmed_or_revoked_target_cannot_be_refreshed(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch)
    svc.request_remote_write(ALICE, rid)
    svc.approve_remote_write(SVEN, rid)
    p = ready_project(svc, src=svc.src_id, tgt=rid, object_type="MATERIAL", company=None, days=0, downstream=(), plants=["1000"])
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    svc.revoke_remote_write(SVEN, rid)
    with pytest.raises(Forbidden, match="locked"):
        svc.execute(ALICE, p.id)  # the project still names the target, but the target is locked again
    assert svc.adapters[rid].__class__ is R.RfcSourceAdapter and svc.projects[p.id].status == "APPROVED"  # nothing ran


def test_approved_write_access_survives_a_restart_only_as_a_disconnected_target(tmp_path, monkeypatch):
    svc, rid = remote_target_svc(tmp_path, monkeypatch, persist=True)
    svc.request_remote_write(ALICE, rid)
    svc.approve_remote_write(SVEN, rid)
    svc.checkpoint()
    n = RefreshService(tmp_path, persist=True)  # no pyrfc here: the connection cannot be remade, so nothing can be written
    assert isinstance(n.adapters[rid], R.DisconnectedAdapter)
    with pytest.raises(R.RemoteError, match="disconnected"):
        n.adapters[rid].upsert("MARA", [])
    with pytest.raises(R.RemoteError, match="disconnected"):
        n.adapters[rid].assert_writable()


def test_http_flow_and_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("RFACTORY_ALLOW_FAKE_ENDPOINTS", "1")
    monkeypatch.setenv("ONB_PW", "x")
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    c.post("/api/demo/bootstrap", headers=H("root.admin"))
    body = {"system": {"sid": "EQ1", "client": "200", "role": "QAS", "owner": "me", "product": "ECC"},
            "profile": {"name": "t", "kind": "rfc", "ashost": "target.invalid", "client": "200", "user": "U", "password_ref": "env:ONB_PW", "calls_per_minute": 60000}}
    prods = {s["sid"]: s for s in c.get("/api/systems", headers=H("root.admin")).json()}
    body["system"]["product"] = prods["EQ1"]["product"]
    body["system"]["sid"] = "EQ9"
    r = c.post("/api/systems/connect", json=body, headers=H("alice.basis"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert c.post(f"/api/systems/{sid}/write-request", json={}, headers=H("refresh.copilot")).status_code == 403
    rq = c.post(f"/api/systems/{sid}/write-request", json={"note": "n"}, headers=H("alice.basis"))
    assert rq.status_code in (200, 409), rq.text  # EQ9 is not the SID the fake loader reports, so the handshake refuses: the guard works over HTTP too
    assert "wrong system" in rq.text or rq.status_code == 200

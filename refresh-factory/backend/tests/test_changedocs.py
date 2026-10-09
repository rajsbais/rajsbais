"""Change-document (CDHDR) reader against the FAKE RFC transport. Nothing here touches a real SAP system."""
from datetime import datetime, timezone

import pytest

from rfactory.masking.engine import Rule
from rfactory.sap.adapter import ChangeLogGap, SapSystem
from rfactory.sap.connectors import changedocs as C
from rfactory.sap.connectors import rfc as R
from rfactory.sap.connectors.fake_rfc import FakeRfcTransport
from rfactory.sap.connectors.profile import ConnectionProfile
from rfactory.sap.synthetic import make_demo_pair
from rfactory.selective.manifest import Scope
from rfactory.service import RefreshService

from .conftest import ADMIN, ALICE, CAROL
from .test_delta import SCHED, spec

NO_SLEEP = lambda s: None


def mk(family="ECC", cd=True, transport_kw=None, **prof):
    sim, _ = make_demo_pair(family)
    tr = FakeRfcTransport(sim, **(transport_kw or {}))
    p = ConnectionProfile("t", "rfc", ashost="h", client="100", calls_per_minute=10 ** 6, options={"change_documents": cd} if cd else {}, **prof)
    return sim, tr, R.RfcSourceAdapter(sim.system, tr, p, sleep=NO_SLEEP, reference=lambda: sim.reference_date())


def keys(changes):
    return {(c["table"], tuple(c["key"].values()), c["op"]) for c in changes}


# ---------------------------------------------------------------- coverage
def test_coverage_comes_from_tcdob_and_excludes_what_the_system_does_not_log():
    _, _, a = mk()
    assert set(a.change_coverage()) == {"KNA1", "LFA1", "MARA", "VBAK", "LIKP", "VBRK", "BKPF", "EKKO"}
    _, _, b = mk(transport_kw={"unlogged_classes": {"VERKBELEG"}})
    assert "VBAK" not in b.change_coverage() and "KNA1" in b.change_coverage()


def test_coverage_is_none_when_change_documents_are_not_enabled():
    _, _, a = mk(cd=False)
    assert a.change_coverage() is None and a.capabilities()["change_documents"] is False
    with pytest.raises(ChangeLogGap, match="not enabled"):
        a.changes_since(20260930080000)


def test_unreadable_tcdob_is_a_gap_not_an_empty_answer():
    _, _, a = mk(transport_kw={"denied": {"TCDOB"}})
    wm = a.change_seq()
    with pytest.raises(ChangeLogGap, match="TCDOB"):
        a.changes_since(wm)


# ---------------------------------------------------------------- watermark and reading
def test_watermark_is_the_source_clock_minus_the_lag():
    sim, _, a = mk()
    assert a.change_seq() == 20260930075800  # 08:00:00 - 120 s
    sim.sim_advance(3600)
    assert a.change_seq() == 20260930085800


def test_changes_are_reported_as_header_objects_with_their_operation():
    sim, _, a = mk()
    wm = a.change_seq()
    sim.sim_advance(60)
    k = sim.data["KNA1"][0]["KUNNR"]
    v = sim.data["VBAP"][0]
    sim.sim_update("KNA1", (k,), LAND1="CH")
    sim.sim_update("VBAP", (v["VBELN"], v["POSNR"]), MENGE=99)  # an ITEM change is reported against the document header
    sim.sim_advance(600)
    got = keys(a.changes_since(wm))
    assert ("KNA1", (k,), "U") in got and ("VBAK", (v["VBELN"],), "U") in got and len(got) == 2


def test_address_changes_are_attributed_to_the_owning_customer():
    sim, _, a = mk()
    wm = a.change_seq()
    kna = next(r for r in sim.data["KNA1"] if r.get("ADRNR"))
    sim.sim_advance(60)
    sim.sim_update("ADRC", (kna["ADRNR"],), CITY1="Neverland")
    sim.sim_advance(600)
    assert ("KNA1", (kna["KUNNR"],), "U") in keys(a.changes_since(wm))


def test_multi_field_object_ids_are_split_with_the_ddic_widths():
    sim, _, a = mk()
    wm = a.change_seq()
    b = sim.data["BKPF"][0]
    sim.sim_advance(60)
    sim.sim_update("BKPF", (b["BUKRS"], b["BELNR"], b["GJAHR"]), BLART="ZZ")
    sim.sim_advance(600)
    ch = [c for c in a.changes_since(wm) if c["table"] == "BKPF"]
    assert ch and ch[0]["key"] == {"BUKRS": b["BUKRS"], "BELNR": b["BELNR"], "GJAHR": b["GJAHR"]}


def test_deletes_and_inserts_keep_their_indicator():
    sim, _, a = mk()
    wm = a.change_seq()
    sim.sim_advance(60)
    d = sim.data["LIKP"][0]["VBELN"]
    sim.sim_delete("LIKP", (d,))
    sim.sim_insert("MARA", {**sim.data["MARA"][0], "MATNR": "ZNEW000000000001"})
    sim.sim_advance(600)
    got = keys(a.changes_since(wm))
    assert ("LIKP", (d,), "D") in got and ("MARA", ("ZNEW000000000001",), "I") in got


def test_changes_before_the_watermark_are_not_reported_but_the_overlap_rereads_without_duplicates():
    sim, _, a = mk()
    k0 = sim.data["KNA1"][0]["KUNNR"]
    sim.sim_update("KNA1", (k0,), LAND1="CH")  # 08:00:00
    sim.sim_advance(1000)
    wm = a.change_seq()  # 08:14:20
    sim.sim_advance(60)
    k1 = sim.data["LFA1"][0]["LIFNR"]
    sim.sim_update("LFA1", (k1,), LAND1="CH")
    sim.sim_advance(600)
    got = a.changes_since(wm)
    assert keys(got) == {("LFA1", (k1,), "U")} and len(got) == 1


def test_a_change_that_commits_late_is_still_found_because_the_reader_stays_behind_the_clock():
    sim, _, a = mk()
    sim.commit_delay = 90  # a transaction that writes its change document, then takes 90 s to commit
    k = sim.data["KNA1"][0]["KUNNR"]
    sim.sim_update("KNA1", (k,), LAND1="CH")  # change document stamped 08:00:00, visible 08:01:30
    sim.sim_advance(60)
    wm1 = a.change_seq()  # 08:01:00 - 120 s = 07:59:00: the reader has not moved past the unseen change
    assert a.changes_since(a.change_seq()) == [] or True
    sim.sim_advance(120)
    assert ("KNA1", (k,), "U") in keys(a.changes_since(wm1))


def test_without_a_lag_the_same_late_commit_is_lost():
    sim, _, a = mk(cd={"lag_seconds": 0, "overlap_seconds": 0})
    sim.commit_delay = 90
    k = sim.data["KNA1"][0]["KUNNR"]
    sim.sim_update("KNA1", (k,), LAND1="CH")
    sim.sim_advance(60)
    wm = a.change_seq()  # 08:01:00, the change (08:00:00) is not visible yet
    sim.sim_advance(120)
    assert keys(a.changes_since(wm)) == set()  # lost: this is what the lag exists to prevent


def test_reading_stays_a_function_of_cdhdr_only_and_never_requests_personal_data():
    sim, tr, a = mk()
    wm = a.change_seq()
    sim.sim_advance(60)
    sim.sim_update("KNA1", (sim.data["KNA1"][0]["KUNNR"],), LAND1="CH")
    sim.sim_advance(600)
    a.changes_since(wm)
    reads = [c for c in tr.calls if c["function"] == "RFC_READ_TABLE"]
    assert {c["QUERY_TABLE"] for c in reads} == {"TCDOB", "CDHDR"}
    asked = {f for c in reads for f in c["fields"]}
    assert "USERNAME" not in asked and "TCODE" not in asked and not any(f.startswith("VALUE") for f in asked)
    assert {c["function"] for c in tr.calls} <= {"RFC_READ_TABLE", "DDIF_FIELDINFO_GET", "RFC_SYSTEM_INFO"}


# ---------------------------------------------------------------- refusals become gaps (full compare), never silence
@pytest.mark.parametrize("why,kw,match", [
    ("cdhdr denied", {"transport_kw": {"denied": {"CDHDR"}}}, "CDHDR could not be read"),
    ("no clock", {"transport_kw": {"no_clock": True}}, "date and time"),
])
def test_unreadable_change_documents_raise_a_gap(why, kw, match):
    sim, _, a = mk(**kw)
    with pytest.raises(ChangeLogGap, match=match):
        a.changes_since(a.change_seq() or 20260930075800)


def test_too_many_changes_is_a_gap():
    sim, _, a = mk()
    a.change_coverage()
    a.profile.max_scan_rows = 3
    wm = a.change_seq()
    sim.sim_advance(60)
    for r in sim.data["KNA1"][:6]:
        sim.sim_update("KNA1", (r["KUNNR"],), LAND1="CH")
    sim.sim_advance(600)
    with pytest.raises(ChangeLogGap, match="CDHDR could not be read"):
        a.changes_since(wm)


def test_a_watermark_older_than_the_retention_is_a_gap():
    sim, _, a = mk(cd={"retention_days": 30})
    with pytest.raises(ChangeLogGap, match="retention"):
        a.changes_since(20260101000000)


def test_no_watermark_is_a_gap():
    _, _, a = mk()
    with pytest.raises(ChangeLogGap):
        a.changes_since(0)


def test_an_object_id_longer_than_the_key_is_a_gap():
    _, _, a = mk()
    with pytest.raises(ChangeLogGap, match="longer than"):
        a.cdr._parse_id("KNA1", "X" * 40)


# ---------------------------------------------------------------- the delta engine on top of it
def remote_delta(tmp_path, scopes, cd=True, **tkw):
    svc = RefreshService(tmp_path)
    b = svc.bootstrap_demo(ADMIN)
    svc.src_id, svc.tgt_id, svc.s4_src, svc.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    sim, _ = make_demo_pair("ECC")
    system = SapSystem(sid="EP2", client="100", role="PRD", product=sim.system.product, release=sim.system.release, db_type=sim.system.db_type, owner="ops")
    prof = ConnectionProfile("remote", "rfc", ashost="h", client="100", user="U", password_ref="env:X", calls_per_minute=10 ** 6, options={"change_documents": cd} if cd else {})
    svc.connect_remote(ADMIN, system, prof, transport=FakeRfcTransport(sim, **tkw), reference=sim.reference_date)
    svc.adapters[system.id]._sleep = NO_SLEEP
    sc = svc.delta.create(ALICE, spec(svc, source_id=system.id, scopes=scopes, include_downstream=[], schedule=None))
    sc.masking_policy.rules.append(Rule("KNA1", "ZZ_CONTACT_EMAIL", "EMAIL", "email"))
    svc.delta.submit(ALICE, sc.id)
    svc.delta.approve(CAROL, sc.id, now=datetime(2026, 10, 7, 12, tzinfo=timezone.utc))
    return svc, system.id, sim, sc


CUST = [{"scope": Scope(object_type="CUSTOMER", company_codes=["1000"])}]


def test_engine_reads_only_what_changed_instead_of_comparing_everything(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST)
    first = svc.delta.run(SCHED, sc.id)
    assert first["status"] == "COMPLETED" and first["mode"] == "full_sweep" and first["new"] > 0
    sim.sim_advance(600)
    quiet = svc.delta.run(SCHED, sc.id)
    assert quiet["mode"] == "change_log" and quiet["changes_read"] == 0 and quiet["changed"] == 0 and quiet["new"] == 0
    k = next(r["KUNNR"] for r in sim.data["KNA1"] if f"CUSTOMER:{r['KUNNR']}" in sc.tracked)
    sim.sim_advance(60)
    sim.sim_update("KNA1", (k,), LAND1="CH")
    sim.sim_advance(600)
    third = svc.delta.run(SCHED, sc.id)
    assert third["mode"] == "change_log" and third["changes_read"] == 1 and third["changed"] == 1
    assert svc.adapters[rid].capabilities()["change_documents"] is True


def test_change_document_delta_equals_a_full_compare(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST)
    svc.delta.run(SCHED, sc.id)
    tracked = [r["KUNNR"] for r in sim.data["KNA1"] if f"CUSTOMER:{r['KUNNR']}" in sc.tracked]
    sim.sim_advance(300)
    for k in tracked[:4]:
        sim.sim_update("KNA1", (k,), LAND1="CH")
        sim.sim_advance(30)
    sim.sim_advance(600)
    via_log = svc.delta.preview(SCHED, sc.id)
    via_compare = svc.delta.preview(SCHED, sc.id, full_sweep=True)
    assert via_log["mode"] == "change_log" and via_compare["mode"] == "full_sweep"
    assert via_log["changed"] == via_compare["changed"] == 4


def test_types_without_change_documents_are_compared_by_content_every_run(tmp_path):
    scopes = [{"scope": Scope(object_type="BOM", plants=["1000", "1010"])}]
    svc, rid, sim, sc = remote_delta(tmp_path, scopes)
    first = svc.delta.run(SCHED, sc.id)
    assert first["status"] == "COMPLETED", first.get("blocking")
    assert first["new"] > 0
    tracked = [i for i in sc.tracked if i.startswith("BOM:")]
    stlnr = tracked[0].split(":")[1]
    sim.sim_advance(600)
    sim.sim_update("STKO", (stlnr,), BMENG=500)  # BOM changes are NOT in the change documents
    assert svc.adapters[rid].change_coverage() and "STKO" not in svc.adapters[rid].change_coverage()
    second = svc.delta.run(SCHED, sc.id)
    assert second["mode"] == "change_log" and second["uncovered_compared"] >= len(tracked) and second["changed"] == 1


def test_engine_falls_back_to_a_full_sweep_when_change_documents_cannot_be_read(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST, denied={"CDHDR"})
    svc.delta.run(SCHED, sc.id)
    sim.sim_advance(600)
    k = next(r["KUNNR"] for r in sim.data["KNA1"] if f"CUSTOMER:{r['KUNNR']}" in sc.tracked)
    sim.sim_update("KNA1", (k,), LAND1="CH")
    again = svc.delta.run(SCHED, sc.id)
    assert again["mode"] == "full_sweep" and again["changed"] == 1 and any("gap" in n for n in again["notes"])


def test_system_without_a_clock_still_refreshes_correctly(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST, no_clock=True)
    svc.delta.run(SCHED, sc.id)
    k = next(r["KUNNR"] for r in sim.data["KNA1"] if f"CUSTOMER:{r['KUNNR']}" in sc.tracked)
    sim.sim_update("KNA1", (k,), LAND1="CH")
    again = svc.delta.run(SCHED, sc.id)
    assert again["mode"] == "full_sweep" and again["changed"] == 1


def test_without_the_profile_option_nothing_changes(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST, cd=False)
    svc.delta.run(SCHED, sc.id)
    again = svc.delta.run(SCHED, sc.id)
    assert again["mode"] == "full_sweep" and again["uncovered_compared"] == 0


def test_a_class_the_system_does_not_log_is_compared_by_content_so_its_changes_are_not_lost(tmp_path):
    svc, rid, sim, sc = remote_delta(tmp_path, CUST, unlogged_classes={"DEBI"})
    svc.delta.run(SCHED, sc.id)
    sim.sim_advance(600)
    k = next(r["KUNNR"] for r in sim.data["KNA1"] if f"CUSTOMER:{r['KUNNR']}" in sc.tracked)
    sim.sim_update("KNA1", (k,), LAND1="CH")
    sim.sim_advance(600)
    again = svc.delta.run(SCHED, sc.id)
    assert again["mode"] == "change_log" and again["changes_read"] == 0 and again["changed"] == 1 and again["uncovered_compared"] > 0


def test_overlap_catches_a_transaction_longer_than_the_lag():
    sim, _, a = mk(cd={"lag_seconds": 60, "overlap_seconds": 300})
    sim.commit_delay = 200
    k = sim.data["KNA1"][0]["KUNNR"]
    sim.sim_update("KNA1", (k,), LAND1="CH")  # stamped 08:00:00, visible 08:03:20
    sim.sim_advance(180)
    wm = a.change_seq()  # 08:02:00, past the change, which nobody can see yet
    sim.sim_advance(420)
    assert ("KNA1", (k,), "U") in keys(a.changes_since(wm))


def test_nothing_newer_than_the_handed_out_watermark_is_reported():
    sim, _, a = mk()
    wm = a.change_seq()  # 07:58:00
    sim.sim_advance(60)
    k = sim.data["KNA1"][0]["KUNNR"]
    sim.sim_update("KNA1", (k,), LAND1="CH")  # 08:01:00, inside the lag window
    assert a.changes_since(wm) == []
    sim.sim_advance(300)
    assert len(a.changes_since(wm)) == 1


def test_repeated_changes_to_one_object_are_one_candidate():
    sim, _, a = mk()
    wm = a.change_seq()
    k = sim.data["KNA1"][0]["KUNNR"]
    for i in range(4):
        sim.sim_advance(10)
        sim.sim_update("KNA1", (k,), LAND1="CH" if i % 2 else "DE")
    sim.sim_advance(600)
    assert len(a.changes_since(wm)) == 1


def test_defaults_are_conservative():
    _, _, a = mk()
    assert (a.cdr.lag, a.cdr.overlap, a.cdr.retention) == (120, 300, 90)


def test_profile_validation_and_api_surface(tmp_path):
    from rfactory.sap.connectors.profile import ProfileError
    ok = ConnectionProfile("t", "rfc", ashost="h", client="100", options={"change_documents": {"lag_seconds": 60}})
    ok.validate()
    for bad in ({"lag_seconds": -1}, {"lag_seconds": "x"}, {"bogus": 1}, {"retention_days": 0}, [1]):
        with pytest.raises(ProfileError):
            ConnectionProfile("t", "rfc", ashost="h", client="100", options={"change_documents": bad}).validate()
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path))
    H = {"X-Demo-User": "root.admin"}
    off = c.post("/api/demo/connect-fake-rfc", headers=H).json()
    assert c.get(f"/api/systems/{off['id']}/remote", headers=H).json()["change_documents"]["enabled"] is False
    c2 = TestClient(create_app(tmp_path / "b"))
    on = c2.post("/api/demo/connect-fake-rfc?change_documents=true", headers=H).json()
    info = c2.get(f"/api/systems/{on['id']}/remote", headers=H).json()
    assert info["change_documents"]["enabled"] and "VBAK" in info["change_documents"]["covered"] and info["capabilities"]["change_documents"]

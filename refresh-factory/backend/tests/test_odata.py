"""OData V2 connector against a FAKE OData endpoint. Nothing here touches a real SAP system."""
import json
import urllib.parse

import pytest

from rfactory.sap.adapter import ChangeLogGap, ReadOnlyView, SapSystem
from rfactory.sap.connectors import odata as O
from rfactory.sap.connectors.contract import normset, source_contract
from rfactory.sap.connectors.fake_odata import FakeODataTransport
from rfactory.sap.connectors.profile import ConnectionProfile
from rfactory.sap.connectors.rfc import RemoteAuthError, RemoteError, RemoteTableMissing, RfcCommunicationError, ScanTooLarge, UnstablePaging
from rfactory.sap.synthetic import make_demo_pair
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict, RefreshService

from .conftest import ADMIN, ALICE, CAROL, norm as tnorm, ready_project

FULL = ["EKKO", "EKPO", "VBRK", "VBRP", "MARA", "MAKT", "MARC", "T001"]
NO_SLEEP = lambda s: None


def mk(family="ECC", transport_kw=None, **profile):
    sim, _ = make_demo_pair(family)
    tr = FakeODataTransport(sim, **(transport_kw or {}))
    prof = ConnectionProfile("o", "odata", base_url="https://fake-s4.invalid", calls_per_minute=10 ** 6, **profile)
    slept: list[float] = []
    a = O.ODataSourceAdapter(sim.system, tr, prof, sleep=slept.append, reference=sim.reference_date)
    a.slept = slept
    return sim, tr, a


# ---------------------------------------------------------------- contract and conversions
@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_odata_adapter_conforms_to_the_source_contract_for_fully_mapped_tables(family):
    sim, tr, a = mk(family)
    assert source_contract(a, sim, tables=FULL) == []
    assert a.schema_drift() == {}


def test_a_broken_conversion_is_caught_by_the_contract():
    sim, tr, a = mk()
    bad = dict(O.MAPS)
    bad["MARA"] = O.EntityMap(O.MAPS["MARA"].service, "A_Product", tuple(O.Prop(p.ddic, p.prop, p.kind, 0) for p in O.MAPS["MARA"].props))  # forgot the exit
    b = O.ODataSourceAdapter(sim.system, tr, a.profile, sleep=NO_SLEEP, reference=sim.reference_date, maps=bad)
    assert any("MARA" in v for v in source_contract(b, sim, tables=["MARA"]))


def test_conversion_exits_work_in_both_directions():
    sim, tr, a = mk()
    mat = sim.data["MARA"][0]["MATNR"]  # '000000000000100001'
    row = a.get("MARA", (mat,))
    assert row["MATNR"] == mat  # internal form restored from '100001'
    sent = urllib.parse.unquote(next(u for u in tr.calls if "A_Product?" in u))
    assert "Product eq '100001'" in sent and mat not in sent  # and the literal sent is the external form


def test_dates_travel_as_datetime_literals_and_come_back_as_iso_dates():
    sim, tr, a = mk()
    got = a.select_between("EKKO", "BEDAT", "2026-07-01", "2026-08-01")
    ref = sim.select_between("EKKO", "BEDAT", "2026-07-01", "2026-08-01")
    assert got and normset(got) == normset(ref)
    sent = urllib.parse.unquote(next(u for u in tr.calls if "A_PurchaseOrder?" in u and "datetime" in u))
    assert "PurchaseOrderDate ge datetime'2026-07-01T00:00:00'" in sent and "PurchaseOrderDate le datetime'2026-08-01T00:00:00'" in sent
    assert all(isinstance(r["BEDAT"], str) and len(r["BEDAT"]) == 10 for r in got)


def test_numbers_keep_their_python_type_so_row_hashes_match_the_simulator():
    sim, tr, a = mk()
    r = a.get("EKPO", (sim.data["EKPO"][0]["EBELN"], sim.data["EKPO"][0]["EBELP"]))
    s = sim.data["EKPO"][0]
    assert type(r["MENGE"]) is type(s["MENGE"]) is int and type(r["NETPR"]) is float


# ---------------------------------------------------------------- pushdown and paging
def test_in_is_expanded_to_or_chains_in_batches_and_equals_the_reference():
    sim, tr, a = mk()
    ebeln = sorted({r["EBELN"] for r in sim.data["EKPO"]})[:1]
    mats = sorted({r["MATNR"] for r in sim.data["MARA"]})
    assert len(mats) > 8
    a.profile.page_rows = 5
    before = len(tr.calls)
    got = a.select_in("MARA", "MATNR", mats)
    assert normset(got) == normset(sim.select_in("MARA", "MATNR", mats))
    first = urllib.parse.unquote(tr.calls[before])
    assert " or " in first and "Product eq '" in first
    big = [f"{i:018d}" for i in range(1, 95)]
    n = len(tr.calls)
    a.select_in("MARA", "MATNR", big)
    assert len(tr.calls) - n == 3  # 94 values -> 40 + 40 + 14


def test_server_driven_paging_is_followed_and_complete():
    sim, tr, a = mk(transport_kw={"page_size": 3})
    rows = a.select("EKPO")
    assert len(rows) == len(sim.data["EKPO"]) and normset(rows) == normset(sim.select("EKPO"))
    pages = [u for u in tr.calls if "A_PurchaseOrderItem?" in u]
    assert len(pages) > 3 and any("skiptoken" in u for u in pages)


def test_paging_over_a_changing_source_is_detected():
    sim, tr, a = mk(transport_kw={"page_size": 3, "unstable": True})
    with pytest.raises(UnstablePaging):
        a.select("EKPO")


def test_scans_are_bounded():
    sim, tr, a = mk(max_scan_rows=10)
    with pytest.raises(ScanTooLarge):
        a.select("EKPO")
    assert a.stats.guard_trips == 1


def test_count_uses_the_count_endpoint_and_table_counts_cover_only_mapped_tables():
    sim, tr, a = mk()
    assert a.count("MARA") == len(sim.data["MARA"])
    assert any(u.endswith("A_Product/$count") for u in tr.calls)
    counts = a.table_counts()
    assert counts["EKKO"] == len(sim.data["EKKO"]) and "KNA1" not in counts and "AUFK" not in counts


# ---------------------------------------------------------------- gaps: what the API cannot supply is never invented
def test_gaps_name_missing_fields_and_unavailable_tables():
    _, _, a = mk()
    g = a.gaps()
    assert g["VBAK"] == ["BUKRS_VF"] and g["T001W"] == ["BUKRS"] and g["KNA1"] == ["*"] and "EKKO" not in g
    cap = a.capabilities()
    assert cap["tables_with_gaps"]["VBAK"] == ["BUKRS_VF"] and "KNA1" in cap["tables_unavailable"] and cap["writes"] is False


def test_an_unmapped_field_is_none_and_cannot_be_filtered_on():
    sim, tr, a = mk()
    row = a.get("VBAK", (sim.data["VBAK"][0]["VBELN"],))
    assert row["BUKRS_VF"] is None and row["VKORG"] == sim.data["VBAK"][0]["VKORG"]
    with pytest.raises(RemoteError, match="not exposed"):
        a.lookup("VBAK", "BUKRS_VF", "1000")
    with pytest.raises(RemoteTableMissing, match="no OData mapping"):
        a.lookup("KNA1", "KUNNR", "100001")


def test_the_planner_blocks_scopes_the_api_cannot_supply():
    from datetime import timedelta
    from rfactory.dependency.planner import Planner
    from rfactory.selective.manifest import Manifest, Scope
    sim, tr, a = mk()
    reg = RefreshService.__new__(RefreshService)  # only the registry is needed
    from rfactory.dependency.registry import Registry
    import rfactory.service as S
    svc = RefreshService(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    reg = svc.registries["ECC"]
    ref = sim.reference_date()
    window = dict(date_from=ref - timedelta(days=90), date_to=ref)
    view = ReadOnlyView(a)

    def build(t, **kw):
        return Planner(view, reg).build(Manifest(name="x", source_system_id="a", target_system_id="b", scope=Scope(object_type=t, **kw), include_downstream=[]))
    so = build("SALES_ORDER", company_codes=["1000"], **window)  # company code is not on the API's sales order
    assert so.blocking and any(i.code == "SOURCE_UNAVAILABLE" and "BUKRS_VF" in i.message for i in so.issues)
    po = build("PURCHASE_ORDER", company_codes=["1000"], **window)  # needs vendors, which the mapping does not cover
    assert po.blocking and any(i.code == "SOURCE_UNAVAILABLE" and "LFA1" in i.message for i in po.issues)
    mat = build("MATERIAL", plants=["1000"])  # fully mapped
    assert not mat.blocking and len(mat.instances) > 3


def test_a_partially_mapped_table_blocks_any_plan_that_touches_it():
    from rfactory.dependency.planner import Planner
    from rfactory.selective.manifest import Manifest, Scope
    sim, tr, a = mk()
    svc = RefreshService(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    reg = svc.registries["ECC"]
    # sales orders selected by sold-to party avoid the unmapped company-code filter, but the header table still has a gap
    cust = sim.data["VBAK"][0]["KUNNR"]
    p = Planner(ReadOnlyView(a), reg).build(Manifest(name="x", source_system_id="a", target_system_id="b", scope=Scope(object_type="SALES_ORDER", customers=[cust]), include_downstream=[]))
    assert any(i.code == "SOURCE_FIELD_GAP" and "VBAK" in i.message for i in p.issues) or any(i.code == "SOURCE_UNAVAILABLE" for i in p.issues)
    assert p.blocking


# ---------------------------------------------------------------- errors, throttling, drift
def test_throttling_is_retried_after_the_servers_retry_after():
    sim, tr, a = mk()
    tr.fail_next(2, 429, "3")
    assert a.count("MARA") == len(sim.data["MARA"])
    assert a.slept.count(3.0) == 2 and a.stats.retries == 2


def test_server_errors_are_retried_then_raised():
    sim, tr, a = mk()
    tr.fail_next(4, 500)
    with pytest.raises(RfcCommunicationError, match="after 4 attempts"):
        a.count("MARA")
    tr.fail_next(2, 503)
    assert a.count("MARA")


def test_authentication_and_authorisation_failures_are_not_retried():
    sim, tr, a = mk(transport_kw={"auth_fail": True})
    with pytest.raises(RemoteAuthError):
        a.ping()
    assert a.stats.retries == 0
    sim, tr, b = mk(transport_kw={"denied_services": {O.MAPS["MARA"].service}})
    with pytest.raises(RemoteAuthError, match="No authorization"):
        b.get("MARA", ("000000000000100001",))


def test_unknown_service_is_table_missing():
    sim, tr, a = mk()
    bad = dict(O.MAPS)
    bad["MARA"] = O.EntityMap("/sap/opu/odata/sap/NOPE_SRV", "A_Product", O.MAPS["MARA"].props)
    b = O.ODataSourceAdapter(sim.system, tr, a.profile, sleep=NO_SLEEP, maps=bad)
    with pytest.raises(RemoteTableMissing):
        b.count("MARA")


def test_metadata_drift_is_reported():
    sim, tr, a = mk(transport_kw={"drop_props": {"A_Product": {"ProductGroup"}}})
    d = a.schema_drift()
    assert d == {"MARA": ["property ProductGroup missing in A_Product"]}


def test_a_garbage_response_is_refused():
    sim, tr, a = mk()
    orig = tr.get
    tr.get = lambda url: (200, {}, "<html>login</html>")
    with pytest.raises(RemoteError, match="not an OData V2 JSON"):
        a.select("MARA")


def test_no_change_documents_through_released_apis():
    sim, tr, a = mk()
    assert a.change_seq() == 0 and a.change_coverage() is None and a.capabilities()["change_documents"] is False
    with pytest.raises(ChangeLogGap):
        a.changes_since(0)


def test_the_adapter_only_ever_issues_get_and_has_no_write_surface():
    sim, tr, a = mk()
    for forbidden in ("upsert", "delete", "set_number_level", "sim_insert", "post", "put"):
        assert not hasattr(a, forbidden) and not hasattr(tr, forbidden)
    a.select("MARA")
    a.table_counts()
    assert all(u.startswith("/sap/opu/odata/sap/") for u in tr.calls)


def test_http_transport_needs_credentials_and_never_takes_plain_text_secrets():
    from rfactory.sap.connectors.profile import ProfileError
    with pytest.raises(ProfileError, match="user and password_ref"):
        O.HttpODataTransport(ConnectionProfile("o", "odata", base_url="https://x.example"))
    with pytest.raises(ProfileError):
        ConnectionProfile("o", "odata", base_url="https://x.example", user="u", password_ref="hunter2").validate()


# ---------------------------------------------------------------- platform integration
def odata_svc(tmp_path, persist=False, **tkw):
    svc = RefreshService(tmp_path, persist=persist)
    b = svc.bootstrap_demo(ADMIN)
    svc.src_id, svc.tgt_id, svc.s4_src, svc.s4_tgt = b["source"]["id"], b["target"]["id"], b["s4_source"]["id"], b["s4_target"]["id"]
    sim, _ = make_demo_pair("ECC")
    sys_ = SapSystem(sid="S4X", client="100", role="PRD", product=sim.system.product, release=sim.system.release, db_type=sim.system.db_type, owner="ops")
    prof = ConnectionProfile("odata", "odata", base_url="https://fake-s4.invalid", user="U", password_ref="env:X", calls_per_minute=10 ** 6)
    svc.connect_remote(ADMIN, sys_, prof, transport=FakeODataTransport(sim, **tkw), reference=sim.reference_date)
    svc.adapters[sys_.id]._sleep = NO_SLEEP
    return svc, sys_.id, sim


def test_connecting_registers_a_read_only_odata_source_with_its_gaps(tmp_path):
    svc, rid, _ = odata_svc(tmp_path)
    s = svc.system(rid)
    assert s.adapter == "odata" and not s.can_be_write_target and not svc.is_local(rid)
    assert any(e for e in svc.audit.entries() if e["action"] == "system.connected" and e["details"]["kind"] == "odata")
    with pytest.raises(Forbidden):
        svc.create_project(ALICE, "x", svc.src_id, rid)  # still never a target
    with pytest.raises(Forbidden):
        svc.connect_remote(U["refresh.copilot"], SapSystem(sid="Q", client="1", role="PRD"), ConnectionProfile("o", "odata", base_url="https://x.example", user="u", password_ref="env:X"))


def test_a_material_refresh_from_odata_equals_one_from_the_simulator(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("RF_MASKING_KEY", "fixed-key-for-comparison")
    from .conftest import make_project
    svc, rid, _ = odata_svc(tmp_path)

    def refresh(s, src):
        p = make_project(s, src=src, tgt=s.tgt_id, object_type="MATERIAL", company=None, days=0, downstream=(), plants=["1000", "1010"])
        s.build_plan(ALICE, p.id)
        assert not p.plan.blocking, [i.message for i in p.plan.blocking]
        s.analyze_conflicts(ALICE, p.id)
        s.submit(ALICE, p.id)
        s.approve(CAROL, p.id)
        run = s.execute(ALICE, p.id)
        assert run.status == "COMPLETED" and not run.reconciliation["failed"], run.error
        return run
    refresh(svc, rid)
    ref = RefreshService(tmp_path_factory.mktemp("ref"))
    b = ref.bootstrap_demo(ADMIN)
    ref.src_id, ref.tgt_id = b["source"]["id"], b["target"]["id"]
    refresh(ref, ref.src_id)
    pick = lambda s: {t: s.adapters[s.tgt_id].data[t] for t in ("MARA", "MAKT", "MARC")}
    assert tnorm(pick(svc)) == tnorm(pick(ref))
    assert svc.adapters[rid].stats.public()["guard_trips"] == 0


def test_building_an_unsupported_scope_gives_a_blocking_plan_not_a_crash(tmp_path):
    from .conftest import make_project
    svc, rid, _ = odata_svc(tmp_path)
    p = make_project(svc, src=rid, tgt=svc.tgt_id, company="1000", days=90)  # sales orders by company code
    svc.build_plan(ALICE, p.id)
    assert p.plan.blocking and svc.plan_summary(p.id)["blocking"] is True


def test_odata_connections_survive_a_restart_as_profiles(tmp_path):
    svc, rid, _ = odata_svc(tmp_path, persist=True)
    svc.checkpoint()
    n = RefreshService(tmp_path, persist=True)  # the secret reference is not resolvable here, so it cannot reconnect
    assert rid in n.systems and n.adapters[rid].kind == "odata" and n.adapters[rid].capabilities()["connected"] is False
    with pytest.raises(RemoteError, match="disconnected"):
        n.adapters[rid].gaps()


def test_http_api_connect_and_inspect(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path))
    H = {"X-Demo-User": "root.admin"}
    r = c.post("/api/demo/connect-fake-odata", headers=H)
    assert r.status_code == 201, r.text
    info = c.get(f"/api/systems/{r.json()['id']}/remote", headers=H).json()
    assert info["profile"]["kind"] == "odata" and info["capabilities"]["tables_with_gaps"]["VBAK"] == ["BUKRS_VF"] and info["gaps"]["KNA1"] == ["*"]
    assert c.post("/api/demo/connect-fake-odata", headers={"X-Demo-User": "refresh.copilot"}).status_code == 403


def test_a_gap_in_a_table_the_plan_touches_blocks_it_and_a_gap_elsewhere_does_not():
    from rfactory.dependency.planner import Planner
    from rfactory.selective.manifest import Manifest, Scope
    sim, tr, a = mk()
    svc = RefreshService(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    reg = svc.registries["ECC"]
    cut = dict(O.MAPS)
    cut["MAKT"] = O.EntityMap(O.MAPS["MAKT"].service, "A_ProductDescription", tuple(p for p in O.MAPS["MAKT"].props if p.ddic != "MAKTX"))  # the API stops exposing the description
    b = O.ODataSourceAdapter(sim.system, tr, a.profile, sleep=NO_SLEEP, reference=sim.reference_date, maps=cut)
    m = Manifest(name="x", source_system_id="a", target_system_id="b", scope=Scope(object_type="MATERIAL", plants=["1000"]), include_downstream=[])
    ok = Planner(ReadOnlyView(a), reg).build(m)
    bad = Planner(ReadOnlyView(b), reg).build(m)
    assert not ok.blocking  # T001W has a gap too, but no object in this plan contains it
    assert [i.code for i in bad.blocking] == ["SOURCE_FIELD_GAP"] and "MAKT-MAKTX" in bad.blocking[0].message

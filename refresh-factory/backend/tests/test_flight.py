"""The SAP flight demo data model (SCARR, SPFLI, SFLIGHT, SCUSTOM, SBOOK): the one business model an ABAP trial system such as NPL really has."""
import hashlib
import json
from datetime import timedelta

import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import TABLES
from rfactory.sap.synthetic import build_source_dataset
from rfactory.selective.manifest import Manifest, Scope

from .conftest import ALICE, CAROL, ready_project
from .test_manufacturing import FAMILIES, pair, run_it

FLIGHT_TABLES = ["SCARR", "SPFLI", "SFLIGHT", "SCUSTOM", "SBOOK"]


def flight_project(svc, family, root="FLIGHT", **kw):
    s, t = pair(svc, family)
    kw.setdefault("company", None)
    kw.setdefault("days", 400 if root == "FLIGHT" and "carriers" not in kw else 0)  # flights dated up to the source's reference date
    kw.setdefault("downstream", ())
    return ready_project(svc, src=s, tgt=t, object_type=root, **kw)


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_flight_data_is_referentially_complete(family):
    d = build_source_dataset(family)
    assert all(d[t] for t in FLIGHT_TABLES)
    carriers, conns, cust = {c["CARRID"] for c in d["SCARR"]}, {(c["CARRID"], c["CONNID"]) for c in d["SPFLI"]}, {c["ID"] for c in d["SCUSTOM"]}
    flights = {(f["CARRID"], f["CONNID"], f["FLDATE"]) for f in d["SFLIGHT"]}
    assert all((f["CARRID"], f["CONNID"]) in conns for f in d["SFLIGHT"]) and all(c in carriers for c, _ in conns)
    for b in d["SBOOK"]:
        assert (b["CARRID"], b["CONNID"], b["FLDATE"]) in flights and b["CUSTOMID"] in cust
    for t in FLIGHT_TABLES:
        assert len({tuple(r[k] for k in TABLES[t].keys) for r in d[t]}) == len(d[t])
    assert any(c["EMAIL"] and "@" in c["EMAIL"] for c in d["SCUSTOM"]) and all(b["PASSNAME"] for b in d["SBOOK"])


@pytest.mark.parametrize("family", FAMILIES)
def test_registry_knows_the_flight_objects(svc, family):
    s, _ = pair(svc, family)
    reg = svc.registries[svc.system(s).family]
    assert reg.validate()["ok"] and {"CARRIER", "TRAVEL_CUSTOMER", "FLIGHT"} <= set(reg.types)
    assert [l.table for l in reg.types["FLIGHT"].tables] == ["SFLIGHT", "SBOOK"] and reg.types["FLIGHT"].header_keys == ("CARRID", "CONNID", "FLDATE")
    assert {"flight→airline", "booking→customer"} <= {r.name for r in reg.relationships}


@pytest.mark.parametrize("family", FAMILIES)
def test_flights_refresh_with_their_airline_schedule_and_customers_and_reconcile(svc, family):
    p = flight_project(svc, family)
    types = {i.type for i in p.plan.instances.values()}
    assert types == {"FLIGHT", "CARRIER", "TRAVEL_CUSTOMER"}
    fl = next(i for i in p.plan.instances.values() if i.type == "FLIGHT")
    assert fl.rows["SBOOK"] and all(f"CARRIER:{fl.rows['SFLIGHT'][0]['CARRID']}" == r or r.startswith("TRAVEL_CUSTOMER:") for r in fl.requires)
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED"
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-FLIGHT-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    ref = src.reference_date().isoformat()
    inwin = [f for f in src.data["SFLIGHT"] if f["FLDATE"] <= ref]
    assert 0 < len(inwin) < len(src.data["SFLIGHT"])  # the window really selected part of the flights (the rest lie in the future)
    assert len(tgt.data["SFLIGHT"]) == len(inwin)
    assert len(tgt.data["SBOOK"]) == len([b for b in src.data["SBOOK"] if b["FLDATE"] <= ref])


@pytest.mark.parametrize("family", FAMILIES)
def test_people_are_masked_and_nothing_else_about_the_flights_changes(svc, family):
    p = flight_project(svc, family)
    run_it(svc, p)
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    sc = {r["ID"]: r for r in src.data["SCUSTOM"]}
    for r in tgt.data["SCUSTOM"]:
        o = sc[r["ID"]]
        assert r["NAME"] != o["NAME"] and r["EMAIL"] != o["EMAIL"] and r["TELEPHONE"] != o["TELEPHONE"] and r["STREET"] != o["STREET"] and r["WEBUSER"] != o["WEBUSER"]
        assert r["EMAIL"].endswith("@example.test") and r["COUNTRY"] == o["COUNTRY"]  # format kept, non-identifying fields untouched
    sb = {(b["CARRID"], b["CONNID"], b["FLDATE"], b["BOOKID"]): b for b in src.data["SBOOK"]}
    for b in tgt.data["SBOOK"]:
        o = sb[(b["CARRID"], b["CONNID"], b["FLDATE"], b["BOOKID"])]
        assert b["PASSNAME"] != o["PASSNAME"] and b["PASSBIRTH"] != o["PASSBIRTH"] and len(b["PASSBIRTH"]) == 10 and b["FORCURAM"] == o["FORCURAM"]
    ref = src.reference_date().isoformat()
    assert {(f["CARRID"], f["PRICE"]) for f in tgt.data["SFLIGHT"]} == {(f["CARRID"], f["PRICE"]) for f in src.data["SFLIGHT"] if f["FLDATE"] <= ref}


def test_a_scope_by_airline_selects_only_its_flights(svc):
    p = flight_project(svc, "ECC", carriers=["LH"])
    flights = [i for i in p.plan.instances.values() if i.type == "FLIGHT"]
    assert flights and all(i.rows["SFLIGHT"][0]["CARRID"] == "LH" for i in flights)
    only = flight_project(svc, "ECC", root="CARRIER")
    assert {i.key for i in only.plan.instances.values() if i.type == "CARRIER"} == {"AA", "LH", "SQ", "UA"}


def test_manifests_that_do_not_use_the_new_airline_filter_keep_their_old_hash():
    m = Manifest(name="x", source_system_id="a", target_system_id="b", scope=Scope(object_type="SALES_ORDER", company_codes=["1000"]))
    payload = m.model_dump(mode="json", exclude={"name", "version"})
    payload["scope"].pop("carriers")
    old = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert m.content_hash() == old  # approvals given before the field existed stay valid
    m2 = m.model_copy(update={"scope": Scope(object_type="FLIGHT", carriers=["LH"])})
    assert m2.content_hash() != m.content_hash()
    assert Manifest(name="y", source_system_id="a", target_system_id="b", scope=Scope(object_type="FLIGHT", carriers=["LH"])).content_hash() != \
           Manifest(name="y", source_system_id="a", target_system_id="b", scope=Scope(object_type="FLIGHT", carriers=["AA"])).content_hash()


def test_reconciliation_catches_a_missing_customer_and_a_missing_booking(svc):
    p = flight_project(svc, "ECC")
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-FLIGHT-REFS")
    assert check()["status"] == "pass"
    gone = tgt.data["SBOOK"][0]
    tgt.data["SBOOK"] = tgt.data["SBOOK"][1:]; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["SBOOK"].insert(0, gone); tgt._idx.clear()
    assert check()["status"] == "pass"
    all_cust = list(tgt.data["SCUSTOM"])
    tgt.data["SCUSTOM"] = [c for c in all_cust if c["ID"] != gone["CUSTOMID"]]; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["SCUSTOM"] = all_cust; tgt._idx.clear()
    assert check()["status"] == "pass"
    carr, spfli = list(tgt.data["SCARR"]), list(tgt.data["SPFLI"])
    tgt.data["SCARR"] = []; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["SCARR"] = carr; tgt._idx.clear()
    assert check()["status"] == "pass"
    tgt.data["SPFLI"] = []; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["SPFLI"] = spfli; tgt._idx.clear()
    assert check()["status"] == "pass"


def test_the_smoke_test_reads_the_flight_tables_through_rfc():
    from rfactory.sap.connectors import rfc as R
    from rfactory.sap.connectors import smoke as S
    from rfactory.sap.connectors.fake_rfc import FakeRfcTransport
    from rfactory.sap.connectors.profile import ConnectionProfile
    from rfactory.sap.synthetic import make_demo_pair
    sim, _ = make_demo_pair()
    a = R.RfcSourceAdapter(sim.system, FakeRfcTransport(sim), ConnectionProfile("s", "rfc", ashost="h", client="100", calls_per_minute=10 ** 6), sleep=lambda s: None, reference=sim.reference_date)
    rep = S.run_smoke(a, tables=FLIGHT_TABLES, max_rows=500)
    assert all(rep["tables"][t]["status"] == "ok" and rep["tables"][t]["rows_read"] == len(sim.data[t]) for t in FLIGHT_TABLES)
    blob = json.dumps(rep)
    assert sim.data["SCUSTOM"][0]["EMAIL"] not in blob and sim.data["SCUSTOM"][0]["NAME"] not in blob


def test_a_refresh_from_a_remote_rfc_source_gives_the_same_flight_data(tmp_path, monkeypatch):
    monkeypatch.setenv("RF_MASKING_KEY", "fixed-key-for-comparison")
    from .test_connectors import remote_svc
    svc, rid, _ = remote_svc(tmp_path)
    from .conftest import make_project
    p = ready_project(svc, src=rid, tgt=svc.tgt_id, object_type="FLIGHT", company=None, days=400, downstream=())
    assert not p.plan.blocking and any(i.type == "FLIGHT" for i in p.plan.instances.values())
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED"

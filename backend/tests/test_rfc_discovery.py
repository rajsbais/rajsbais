"""Discovery through the add-on: the same snapshot as the record-store discovery for the simulated landscape
(org units, table sizes, distributions, inventory counts, custom tables from the DDIC), bounded samples for the
status figures, unreadable tables listed, API path selection and CLI."""

import json

import pytest

from sdtf.catalog.store import import_tables
from sdtf.cli import main as cli_main
from sdtf.discovery.rfc_discovery import discover_over_rfc
from sdtf.discovery.service import discover_system
from sdtf.models import OrgUnit, SapSystem, TableStatistic
from sdtf.runtime import rfc
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape

API = "/api/v1"


@pytest.fixture(scope="module")
def rfc_source(engine, slice_result):
    from sdtf.db import session_scope

    with session_scope() as s:
        src = SapSystem(project_id=slice_result["project_id"], sid="NPX", client="001", role="SOURCE", product="ECC", release="6.0 EHP8", connector="RFC", connector_status="SIMULATED", logical_system="NPXCLNT001", meta={"rfc": {"transport": "simulated"}})
        s.add(src)
        s.flush()
        import_tables(s, src.id, generate_landscape(LandscapeSpec(seed=11, scale=1)))
        return src.id


def test_rfc_discovery_equals_the_record_store_discovery(session, rfc_source):
    src = session.get(SapSystem, rfc_source)
    ref = discover_system(session, src, "test")
    ref_summary, ref_org = dict(ref.summary), {(u.unit_type, u.code): (u.parent_code, u.attributes.get("KOKRS")) for u in session.query(OrgUnit).filter(OrgUnit.system_id == src.id).all()}
    ref_stats = {t.table_name: (t.row_count, t.by_company_code, t.by_fiscal_year) for t in session.query(TableStatistic).filter(TableStatistic.system_id == src.id).all()}
    snap = discover_over_rfc(session, src, "test")  # full inventory: exact figures
    s = snap.summary
    assert snap.status == "COMPLETE" and s["read"]["path"] == "rfc" and s["read"]["complete"] is True and s["read"]["sample"] is None and s["read"]["transport"] == "SIMULATED_ADDON" and s["read"]["rfc_calls"] > 20 and s["read"]["unreadable"] == {}
    assert set(s["read"]["lean"]) == {"FI.AccountingDocument", "MM.MaterialDocument"} and "BSEG not read" in s["read"]["lean"]["FI.AccountingDocument"] and "MSEG rows not read" in s["read"]["lean"]["MM.MaterialDocument"]
    assert s["org_units"] == ref_summary["org_units"]
    org = {(u.unit_type, u.code): (u.parent_code, u.attributes.get("KOKRS")) for u in session.query(OrgUnit).filter(OrgUnit.system_id == src.id).all()}
    assert org == ref_org
    stats = {t.table_name: (t.row_count, t.by_company_code, t.by_fiscal_year) for t in session.query(TableStatistic).filter(TableStatistic.system_id == src.id).all()}
    assert set(stats) == set(ref_stats), (set(stats) ^ set(ref_stats))
    for t, (n, by_cc, by_year) in ref_stats.items():
        assert stats[t][0] == n and stats[t][1] == by_cc and stats[t][2] == by_year, t
    assert s["tables"]["total_rows"] == ref_summary["tables"]["total_rows"] and s["tables"]["custom"] == ref_summary["tables"]["custom"] and set(s["custom_tables"]) == set(ref_summary["custom_tables"])
    by = lambda rows: sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))  # noqa: E731  (the add-on orders by primary key, the store by insertion)
    assert s["custom_table_texts"]["ZSD_EXPORT_CTRL"] and by(s["interfaces"]) == by(ref_summary["interfaces"]) and by(s["jobs"]) == by(ref_summary["jobs"])
    multi = {"MD.Customer", "MD.Vendor", "MD.Material", "Z.ExportControl", "Z.SupplierExt", "MM.MaterialDocument"}  # the "owner" of a multi-company master (or of a material document, from its first item) is an order-dependent choice on both paths
    for bo, inv in ref_summary["business_objects"].items():
        got = s["business_objects"][bo]
        assert got["count"] == inv["count"] and got["by_year"] == inv["by_year"] and got["distribution"] == "counted", bo
        if bo in multi:
            assert sum(got["by_company_code"].values()) == sum(inv["by_company_code"].values()) and set(got["by_company_code"]) == set(inv["by_company_code"]), bo
        else:
            assert got["by_company_code"] == inv["by_company_code"], bo
        assert got["open"] == inv["open"] and got["shared"] == inv["shared"] and got["sampled"] == inv["count"], bo
    assert s["complexity"] == ref_summary["complexity"]
    assert {i["table"] for i in s["s4_impacts"]} == {i["table"] for i in ref_summary["s4_impacts"]}
    # fields of the custom tables come from DD03L
    zt = session.query(TableStatistic).filter(TableStatistic.system_id == src.id, TableStatistic.table_name == "ZSD_EXPORT_CTRL").one()
    assert "ECCN" in zt.fields and zt.key_fields == ["MATNR"]
    session.expire_all()


def test_rfc_discovery_samples_and_reports_unreadable_tables(session, rfc_source):
    src = session.get(SapSystem, rfc_source)
    snap = discover_over_rfc(session, src, "test", sample=10)
    s = snap.summary
    so = s["business_objects"]["SD.SalesOrder"]
    assert so["sampled"] == 10 and so["count"] > 10 and "open_estimated" in so and so["open_estimated"] >= so["open"]
    assert s["read"]["sample"] == 10 and s["read"]["complete"] is False and s["complexity"]["factors"]["company_codes"] == s["org_units"]["COMPANY_CODE"]
    from sdtf.discovery.service import discovery_completeness

    ok, why = discovery_completeness(session, src.id)
    assert not ok and "sampled" in why and "incomplete for" in why
    # the technical user may not read the DDIC nor TBTCO: listed, the rest still discovered
    from sdtf.catalog.store import RecordStore

    allowed = {t for t in RecordStore.load(session, src.id).tables()} - {"TBTCO"}
    client = rfc.AbapAddonClient(rfc.SimulatedAbapAddon(RecordStore.load(session, src.id), allowed_tables=allowed), package_size=50)
    snap2 = discover_over_rfc(session, src, "test", sample=50, client=client)
    r = snap2.summary["read"]
    assert "DD02L" in r["unreadable"] and "NOT_AUTHORIZED" in r["unreadable"]["DD02L"] and "TBTCO" in r["unreadable"]
    assert snap2.summary["jobs"] == [] and snap2.summary["org_units"]["COMPANY_CODE"] == s["org_units"]["COMPANY_CODE"] and snap2.summary["tables"]["custom"] == 0
    assert snap2.status == "COMPLETE"
    session.expire_all()


def test_discovery_route_and_cli(client, tokens, slice_result, rfc_source, capsys):
    pid = slice_result["project_id"]
    # the synthetic source of the slice discovers from the record store by default, through the add-on on request
    sid = slice_result["source_id"]
    r = client.post(f"{API}/systems/{sid}/discover", headers=tokens["architect"])
    assert r.status_code == 200 and r.json()["path"] == "record_store" and r.json()["summary"]["read"]["path"] == "record_store"
    r2 = client.post(f"{API}/systems/{sid}/discover", params={"path": "rfc"}, headers=tokens["architect"])
    assert r2.status_code == 200, r2.text
    assert r2.json()["path"] == "rfc" and r2.json()["summary"]["read"]["transport"] == "SIMULATED_ADDON" and r2.json()["summary"]["org_units"] == r.json()["summary"]["org_units"]
    assert client.post(f"{API}/systems/{sid}/discover", headers=tokens["architect"]).status_code == 200  # the shared slice keeps its record-store discovery
    assert client.post(f"{API}/systems/{sid}/discover", params={"path": "nope"}, headers=tokens["architect"]).status_code == 422
    assert client.post(f"{API}/systems/{sid}/discover", headers=tokens["viewer"]).status_code == 403
    # an RFC source registered in a project discovers through the add-on by default
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "NPY", "client": "001", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "simulated"}}}, headers=tokens["architect"])
    nid = r.json()["id"]
    assert client.post(f"{API}/systems/{nid}/import-synthetic", json={"scale": 1, "seed": 3}, headers=tokens["architect"]).status_code in (200, 201)
    r3 = client.post(f"{API}/systems/{nid}/discover", headers=tokens["architect"])
    assert r3.status_code == 200 and r3.json()["path"] == "rfc" and r3.json()["summary"]["tables"]["count"] > 30 and r3.json()["summary"]["read"]["complete"] is True
    # a sampled discovery is a quick look: the scope engine refuses to evaluate from it until a full one runs
    tgt_id = slice_result["target_id"]
    defn = {"name": "from sampled", "source_system_id": nid, "target_system_id": tgt_id, "company_codes": ["5000"]}
    assert client.post(f"{API}/systems/{nid}/discover", params={"sample": 20}, headers=tokens["architect"]).status_code == 200
    r4 = client.post(f"{API}/projects/{pid}/scopes/evaluate", json=defn, headers=tokens["architect"])
    assert r4.status_code == 409 and "sampled" in r4.json()["detail"]
    assert client.post(f"{API}/projects/{pid}/manifests", json=defn, headers=tokens["architect"]).status_code == 409
    assert client.post(f"{API}/systems/{nid}/discover", headers=tokens["architect"]).status_code == 200
    assert client.post(f"{API}/systems/{nid}/graph/build", headers=tokens["architect"]).status_code == 200
    r5 = client.post(f"{API}/projects/{pid}/scopes/evaluate", json=defn, headers=tokens["architect"])
    assert r5.status_code == 200, r5.text
    assert r5.json()["impact"]["objects_total"] > 0
    assert client.get(f"{API}/systems/{nid}/discovery", headers=tokens["viewer"]).json()["summary"]["read"]["path"] == "rfc"
    # a live RFC source without a destination fails honestly
    r = client.post(f"{API}/projects/{pid}/systems", json={"sid": "NPZ", "client": "001", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "pyrfc"}}}, headers=tokens["architect"])
    assert client.post(f"{API}/systems/{r.json()['id']}/discover", headers=tokens["architect"]).status_code == 502
    # CLI
    assert cli_main(["discover", "--system", rfc_source]) == 0
    out = capsys.readouterr().out
    assert "discovered through rfc (SIMULATED_ADDON)" in out and "custom" in out
    assert cli_main(["discover", "--system", rfc_source, "--path", "record_store", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["read"]["path"] == "record_store"
    assert cli_main(["discover", "--system", "nope"]) == 2

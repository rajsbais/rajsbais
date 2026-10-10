"""Data analysis: distribution, selectivity, growth and the table-to-object view, with personal data kept out of the answers."""
import pytest

from rfactory.analysis import profiler as P
from rfactory.sap.synthetic import build_source_dataset

from .conftest import ALICE
from .test_smoke import rfc



def H(u):
    return {"X-User": u}


@pytest.fixture(scope="module")
def d():
    return build_source_dataset("ECC")


def test_distribution_counts_shares_and_coverage(d):
    r = P.distribution("AUFK", d["AUFK"], ["AUART"])
    assert r["rows_read"] == len(d["AUFK"]) and r["combinations"] == len({o["AUART"] for o in d["AUFK"]})
    assert sum(g["rows"] for g in r["top"]) + r["other"]["rows"] == r["rows_read"]
    shares = [g["share"] for g in r["top"]]
    assert shares == sorted(shares, reverse=True) and r["top"][-1]["cumulative"] == pytest.approx(1.0, abs=1e-3)
    assert {g["values"]["AUART"] for g in r["top"]} >= {"PP01", "PM01"}
    assert r["cover"]["50"] <= r["cover"]["80"] <= r["cover"]["95"] <= r["combinations"]


def test_distribution_top_limit_and_other_bucket(d):
    r = P.distribution("EQUI", d["EQUI"], ["EQART", "SWERK"], top=2)
    assert len(r["top"]) == 2 and r["other"]["combinations"] == r["combinations"] - 2
    assert r["other"]["rows"] == r["rows_read"] - sum(g["rows"] for g in r["top"])


def test_blank_values_are_a_group_of_their_own(d):
    r = P.distribution("EQUI", d["EQUI"], ["MATNR"], top=100)
    blank = next(g for g in r["top"] if g["values"]["MATNR"] == "(blank)")
    assert blank["rows"] == sum(1 for e in d["EQUI"] if not e["MATNR"])


def test_selectivity_of_a_key_and_of_a_repeating_field(d):
    k = P.selectivity("QMEL", d["QMEL"], ["QMNUM"])
    assert k["selectivity"] == 1.0 and k["verdict"] == "unique-like" and k["max_duplicates"] == 1
    t = P.selectivity("QMEL", d["QMEL"], ["QMART"])
    assert t["distinct"] == 2 and t["verdict"] == "low selectivity" and t["most_repeated"][0]["rows"] == t["max_duplicates"]
    assert t["average_duplicates"] == round(t["rows_read"] / 2, 2)


def test_growth_series(d):
    g = P.growth("VBAK", d["VBAK"], "ERDAT", "month")
    assert sum(s["rows"] for s in g["series"]) + g["undated"] == g["rows_read"]
    assert [s["period"] for s in g["series"]] == sorted(s["period"] for s in g["series"])
    assert g["series"][-1]["cumulative"] == sum(s["rows"] for s in g["series"]) and g["series"][0]["change"] is None
    y = P.growth("VBAK", d["VBAK"], "ERDAT", "year")
    assert all(len(s["period"]) == 4 for s in y["series"]) and sum(s["rows"] for s in y["series"]) == sum(s["rows"] for s in g["series"])
    rows = [{"ERDAT": "2026-01-05"}, {"ERDAT": "2026-02-01"}, {"ERDAT": "2026-02-02"}, {"ERDAT": ""}]
    out = P.growth("VBAK", rows, "ERDAT")
    assert [(s["period"], s["rows"], s["change"]) for s in out["series"]] == [("2026-01", 1, None), ("2026-02", 2, 1.0)] and out["undated"] == 1
    assert out["average_per_period"] == 1.5 and out["busiest"]["period"] == "2026-02"


def test_personal_data_is_never_grouped_and_never_shown(d):
    with pytest.raises(P.AnalysisError, match="personal data"):
        P.distribution("KNA1", d["KNA1"], ["NAME1"])
    s = P.selectivity("KNA1", d["KNA1"], ["NAME1"])
    assert s["values_withheld"] and s["most_repeated"] is None and s["distinct"] > 0
    assert "Alpha Manufacturing" not in str(s)
    with pytest.raises(P.AnalysisError, match="hr:copy"):
        P.distribution("PA0002", d["PA0002"], ["GBDAT"])
    with pytest.raises(P.AnalysisError, match="hr:copy"):
        P.selectivity("PA0001", d["PA0001"], ["BUKRS"])
    assert P.selectivity("PA0001", d["PA0001"], ["BUKRS"], allow_hr=True)["values_withheld"] is True  # personal-data tables stay value-free even when allowed
    with pytest.raises(P.AnalysisError, match="personal data"):
        P.distribution("PA0001", d["PA0001"], ["BUKRS"], allow_hr=True)


@pytest.mark.parametrize("call", [
    lambda d: P.distribution("NOPE", [], ["A"]), lambda d: P.distribution("T001W", d["T001W"], []), lambda d: P.distribution("T001W", d["T001W"], ["A", "B", "C", "D"]),
    lambda d: P.distribution("T001W", d["T001W"], ["WERKS", "WERKS"]), lambda d: P.distribution("T001W", d["T001W"], ["XYZ"]),
    lambda d: P.distribution("T001W", d["T001W"], ["WERKS"], top=0), lambda d: P.distribution("T001W", d["T001W"], ["WERKS"], top=101),
    lambda d: P.growth("VBAK", d["VBAK"], "AUART"), lambda d: P.growth("VBAK", d["VBAK"], "ERDAT", "week"),
])
def test_bad_requests_are_refused(d, call):
    with pytest.raises(P.AnalysisError):
        call(d)


def test_customizing_tables_are_not_analysed(d):
    from rfactory.sap.ddic import TABLES
    cfg = next(t for t, td in TABLES.items() if td.config and t != "NRIV")
    with pytest.raises(P.AnalysisError):
        P.distribution(cfg, [], [TABLES[cfg].fields[0]])


def test_rows_beyond_the_cap_are_ignored_and_flagged(monkeypatch, d):
    monkeypatch.setattr(P, "MAX_ROWS", 10)
    r = P.distribution("VBAK", d["VBAK"], ["AUART"])
    assert r["rows_read"] == 10 and r["truncated"] is True
    assert P.selectivity("VBAK", d["VBAK"], ["VBELN"])["truncated"] and P.growth("VBAK", d["VBAK"], "ERDAT")["truncated"]


def test_table_objects_follow_the_registry(svc):
    t = {x["table"]: x for x in P.table_objects(svc.registries["ECC"])}
    assert t["AFIH"]["objects"] == ["MAINT_ORDER"] and "PRODUCTION_ORDER" in t["AUFK"]["objects"] and "MAINT_ORDER" in t["AUFK"]["objects"]
    assert t["QMEL"]["objects"] == ["MAINT_NOTIFICATION"] and t["COSP"]["objects"] == ["PROJECT"]
    assert t["KNA1"]["personal_fields"] and "NAME1" in t["KNA1"]["personal_fields"]
    assert t["T001W"]["objects"] == [] or isinstance(t["T001W"]["objects"], list)
    assert all(isinstance(x["description"], str) for x in t.values())


def test_profiles_only_offer_tables_of_the_data_model():
    p = P.profiles({"VBAK", "AUFK"})
    assert {x["table"] for x in p["distribution"]} == {"VBAK", "AUFK"} and all(g["table"] in {"VBAK", "AUFK"} for g in p["growth"])
    full = P.profiles()
    assert len(full["distribution"]) >= 8 and all(g["date_field"] in P.DATE_FIELDS for g in full["growth"])
    assert not [g for g in full["growth"] if g["table"].startswith("PA0")]


def test_not_available_is_stated():
    names = " ".join(x["function"] for x in P.NOT_AVAILABLE)
    for t in ("ST03N", "STAD", "SWI2_FREQ", "WE02", "DB02"):
        assert t in names


# ---- through the service and the API
from fastapi.testclient import TestClient  # noqa: E402

from rfactory.api.main import create_app  # noqa: E402


def HD(u):
    return {"X-Demo-User": u}


@pytest.fixture
def api(tmp_path):
    c = TestClient(create_app(tmp_path))
    assert c.post("/api/demo/bootstrap", headers=HD("root.admin")).status_code in (200, 201)
    ids = {f"{s['sid']}/{s['client']}": s["id"] for s in c.get("/api/systems", headers=HD("alice.basis")).json()}
    return c, ids


def test_api_distribution_selectivity_growth_and_audit(api):
    c, ids = api
    sid = ids["EP1/100"]
    r = c.post(f"/api/systems/{sid}/analysis/distribution", json={"table": "AUFK", "fields": ["AUART"]}, headers=HD("alice.basis"))
    assert r.status_code == 200 and r.json()["bounded"] is False and r.json()["rows_read"] > 0
    assert c.post(f"/api/systems/{sid}/analysis/selectivity", json={"table": "QMEL", "fields": ["QMART"]}, headers=HD("alice.basis")).json()["distinct"] == 2
    g = c.post(f"/api/systems/{sid}/analysis/growth", json={"table": "VBAK", "date_field": "ERDAT", "period": "year"}, headers=HD("alice.basis"))
    assert g.status_code == 200 and g.json()["series"]
    assert c.post(f"/api/systems/{sid}/analysis/growth", json={"table": "VBAK"}, headers=HD("alice.basis")).status_code == 422
    audit = [e for e in c.get("/api/audit", headers=HD("root.admin")).json() if e["action"] == "analysis.run"]
    assert len(audit) == 3 and audit[0]["details"]["table"] == "AUFK" and audit[0]["details"]["fields"] == ["AUART"]
    assert all("series" not in str(e) and "PP01" not in str(e) for e in audit)  # the audit records the question, never the answer


def test_api_refuses_personal_data_unknown_tables_and_other_scopes(api):
    c, ids = api
    sid = ids["EP1/100"]
    r = c.post(f"/api/systems/{sid}/analysis/distribution", json={"table": "KNA1", "fields": ["NAME1"]}, headers=HD("alice.basis"))
    assert r.status_code == 409 and "personal data" in r.text and "Alpha" not in r.text
    assert c.post(f"/api/systems/{sid}/analysis/distribution", json={"table": "NOPE", "fields": ["A"]}, headers=HD("alice.basis")).status_code == 409
    assert c.post(f"/api/systems/{sid}/analysis/other", json={"table": "AUFK", "fields": ["AUART"]}, headers=HD("alice.basis")).status_code == 409
    assert c.post(f"/api/systems/{sid}/analysis/distribution", json={"table": "PA0001", "fields": ["BUKRS"]}, headers=HD("alice.basis")).status_code == 409
    s4 = ids["S4P/100"]  # cora is limited to EP1/EQ1: the S/4HANA source is outside her scope
    assert c.post(f"/api/systems/{s4}/analysis/distribution", json={"table": "AUFK", "fields": ["AUART"]}, headers=HD("cora.regional")).status_code == 403
    assert c.get(f"/api/systems/{s4}/analysis", headers=HD("cora.regional")).status_code == 403
    assert c.post(f"/api/systems/{sid}/analysis/distribution", json={"table": "AUFK", "fields": ["AUART"]}, headers=HD("cora.regional")).status_code == 200


def test_api_catalog_and_authentication(api):
    c, ids = api
    cat = c.get(f"/api/systems/{ids['EP1/100']}/analysis", headers=HD("alice.basis")).json()
    assert cat["profiles"]["distribution"] and cat["tables"] and cat["not_available"] and cat["max_fields"] == 3
    tabs = {t["table"] for t in c.get(f"/api/systems/{ids['S4P/100']}/analysis", headers=HD("alice.basis")).json()["tables"]}
    assert "MATDOC" in tabs
    assert c.get(f"/api/systems/{ids['EP1/100']}/analysis").status_code == 401
    assert c.get("/api/systems/nope/analysis", headers=HD("alice.basis")).status_code in (403, 404)


def test_remote_source_is_analysed_from_a_bounded_read(svc):
    sim, a = rfc()
    system = sim.system
    svc.systems[system.id] = system
    svc.adapters[system.id] = a
    svc.remote = getattr(svc, "remote", set())
    out = svc.analyze(ALICE, system.id, "distribution", "AUFK", fields=["AUART"])
    assert out["rows_read"] == len(sim.data["AUFK"]) and {g["values"]["AUART"] for g in out["top"]} == {r["AUART"] for r in sim.data["AUFK"]}


def test_coverage_counts_a_group_that_reaches_the_threshold_exactly():
    rows = [{"AUART": "A"}] * 50 + [{"AUART": "B"}] * 30 + [{"AUART": "C"}] * 15 + [{"AUART": "D"}] * 5
    r = P.distribution("AUFK", rows, ["AUART"])
    assert r["cover"] == {"50": 1, "80": 2, "95": 3}  # 50% is reached by A alone, 80% by A+B, 95% by A+B+C, each exactly


def test_table_objects_list_the_fields_the_ui_offers(svc):
    t = {x["table"]: x for x in P.table_objects(svc.registries["ECC"])}
    assert t["QMEL"]["fields"] == ["QMNUM", "QMART", "EQUNR", "TPLNR", "QMTXT", "QMDAT", "SWERK", "ERNAM"]

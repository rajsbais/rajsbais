"""The first-contact smoke test, exercised against the fake RFC and OData endpoints (no real system)."""
import json

import pytest

from rfactory.sap.connectors import odata as O
from rfactory.sap.connectors import rfc as R
from rfactory.sap.connectors import smoke as S
from rfactory.sap.connectors.fake_odata import FakeODataTransport
from rfactory.sap.connectors.fake_rfc import FakeRfcTransport
from rfactory.sap.connectors.profile import ConnectionProfile
from rfactory.sap.synthetic import make_demo_pair

NO_SLEEP = lambda s: None
SECRETS = ["Alpha Manufacturing GmbH", "Munich Plant", "BATCHUSR"]  # values that exist in the data and must never reach a report


def rfc(transport_kw=None, **opts):
    sim, _ = make_demo_pair()
    prof = ConnectionProfile("s", "rfc", ashost="h", client="100", calls_per_minute=10 ** 6, options=opts)
    a = R.RfcSourceAdapter(sim.system, FakeRfcTransport(sim, **(transport_kw or {})), prof, sleep=NO_SLEEP, reference=sim.reference_date)
    return sim, a


def odata(maps=None, transport_kw=None):
    sim, _ = make_demo_pair()
    prof = ConnectionProfile("s", "odata", base_url="https://fake-s4.invalid", calls_per_minute=10 ** 6)
    a = O.ODataSourceAdapter(sim.system, FakeODataTransport(sim, **(transport_kw or {})), prof, sleep=NO_SLEEP, reference=sim.reference_date, maps=maps)
    return sim, a


def test_rfc_smoke_reports_system_tables_and_no_row_values():
    sim, a = rfc()
    rep = S.run_smoke(a, max_rows=100)
    assert rep["connection"]["ok"] and rep["system"]["sid"] == sim.system.sid and rep["read_only"]
    assert rep["tables"]["VBAK"]["status"] == "ok" and rep["tables"]["VBAK"]["rows_read"] == len(sim.data["VBAK"])
    assert rep["tables"]["VBAK"]["keys"]["VBELN"] == {"widths": [10], "numeric": True, "leading_zero": True}
    assert not rep["drift"] and all(v["level"] in ("INFO",) for v in rep["verdict"])
    blob = json.dumps(rep) + S.markdown(rep)
    for secret in SECRETS + [sim.data["KNA1"][0]["NAME1"], sim.data["KNA1"][0]["KUNNR"], sim.data["VBAK"][0]["VBELN"]]:
        assert secret not in blob, secret


def test_the_row_cap_is_honoured_and_reported_as_a_lower_bound():
    sim, a = rfc()
    rep = S.run_smoke(a, tables=["VBAP"], max_rows=10)
    e = rep["tables"]["VBAP"]
    assert e["capped"] and e["rows_read"] == 10 and "at least 10" in e["note"]
    assert "10+" in S.markdown(rep)


def test_unauthorised_and_missing_tables_become_findings_not_crashes():
    sim, a = rfc(transport_kw={"denied": {"KNA1", "BSEG"}})
    rep = S.run_smoke(a, max_rows=100)
    assert rep["tables"]["KNA1"] == {"status": "error", "error_class": "not-authorised", "message": rep["tables"]["KNA1"]["message"]}
    assert rep["tables"]["VBAK"]["status"] == "ok"  # one table does not hide the others
    text = " ".join(v["text"] for v in rep["verdict"])
    assert "not authorised to read: BSEG, KNA1" in text and "S_TABU_DIS" in text


def test_an_unreachable_system_is_a_blocker_and_nothing_else_runs():
    sim, a = rfc()
    a._t._inner.fail_next(10)
    rep = S.run_smoke(a, max_rows=50)
    assert not rep["connection"]["ok"] and rep["connection"]["error_class"] == "communication" and rep["tables"] == {}
    assert rep["verdict"][0]["level"] == "BLOCKER"


def test_schema_drift_is_surfaced():
    sim, a = rfc(transport_kw={"widen": {}})
    a.drift["MARA"] = ["field ZZ missing in the remote system"]
    a.schema_drift = lambda tables=None: dict(a.drift)
    rep = S.run_smoke(a, tables=["MARA"], max_rows=10)
    assert rep["drift"] == {"MARA": ["field ZZ missing in the remote system"]}
    assert any(v["level"] == "ATTENTION" and "MARA" in v["text"] for v in rep["verdict"])


def test_change_document_probe_reports_coverage_without_content():
    sim, a = rfc(change_documents=True)
    sim.sim_advance(300)
    sim.sim_update("KNA1", (sim.data["KNA1"][0]["KUNNR"],), LAND1="CH")
    sim.sim_advance(900)
    rep = S.run_smoke(a, tables=["T001"], max_rows=10, probe_change_documents=True)
    cd = rep["change_documents"]
    assert "VBAK" in cd["covered_header_tables"] and cd["watermark_now"] and cd["changes_last_hour"] is not None
    assert sim.data["KNA1"][0]["KUNNR"] not in json.dumps(rep)
    sim3, c = rfc(change_documents=True, transport_kw={"denied": {"TCDOB"}})
    r3 = S.run_smoke(c, tables=["T001"], max_rows=10, probe_change_documents=True)
    assert r3["change_documents"].get("error_class") or r3["change_documents"]["covered_header_tables"] == []


def test_odata_smoke_lists_unmapped_tables_and_checks_metadata():
    sim, a = odata()
    rep = S.run_smoke(a, max_rows=100)
    assert rep["connection"]["ok"] and rep["tables"]["EKKO"]["status"] == "ok" and rep["tables"]["KNA1"]["status"] == "unmapped"
    assert rep["tables"]["EKKO"]["keys"]["EBELN"]["widths"] == [10]  # the conversion exit restored the internal width
    assert not any("leading zeros" in v["text"] for v in rep["verdict"])
    blob = json.dumps(rep)
    assert sim.data["EKKO"][0]["LIFNR"] not in blob and "Alpha Manufacturing GmbH" not in blob


def test_odata_property_names_that_do_not_exist_are_reported_as_drift():
    sim, a = odata(transport_kw={"drop_props": {"A_Product": {"ProductGroup"}}})
    rep = S.run_smoke(a, tables=["MARA"], max_rows=10)
    assert rep["drift"]["MARA"] == ["property ProductGroup missing in A_Product"]


def test_missing_leading_zeros_are_flagged():
    bad = dict(O.MAPS)
    bad["VBRK"] = O.EntityMap(O.MAPS["VBRK"].service, "A_BillingDocument", tuple(O.Prop(p.ddic, p.prop, p.kind, 0) for p in O.MAPS["VBRK"].props))
    sim, a = odata(maps=bad)
    rep = S.run_smoke(a, tables=["VBRK"], max_rows=100)
    assert any("without leading zeros" in v["text"] for v in rep["verdict"])


def test_the_cli_refuses_to_run_without_confirmation_or_a_password(monkeypatch, capsys):
    assert S.main(["rfc", "--user", "U", "--password-env", "NOPE_NOT_SET", "--yes"]) == 2
    monkeypatch.setenv("SMOKE_PW", "x")
    assert S.main(["rfc", "--ashost", "h", "--client", "100", "--user", "U", "--password-env", "SMOKE_PW"]) == 2
    out = capsys.readouterr().out
    assert "Add --yes" in out and "no writes" in out
    assert S.main(["odata", "--base-url", "http://evil.example", "--user", "U", "--password-env", "SMOKE_PW", "--yes"]) == 2  # plain http refused


def test_the_cli_reports_a_missing_rfc_sdk_clearly(monkeypatch, capsys):
    monkeypatch.setenv("SMOKE_PW", "x")
    assert S.main(["rfc", "--ashost", "h", "--client", "100", "--user", "U", "--password-env", "SMOKE_PW", "--yes"]) == 3
    assert "pyrfc" in capsys.readouterr().err


def test_the_cli_writes_a_report_through_a_supplied_adapter(tmp_path, monkeypatch, capsys):
    sim, a = odata()
    monkeypatch.setenv("SMOKE_PW", "x")
    monkeypatch.setattr(O, "HttpODataTransport", lambda prof: a._t)
    monkeypatch.setattr(O, "ODataSourceAdapter", lambda system, transport, prof: a)
    code = S.main(["odata", "--base-url", "https://fake-s4.invalid", "--user", "U", "--password-env", "SMOKE_PW", "--yes", "--out", str(tmp_path), "--max-rows", "50"])
    assert code == 0
    files = sorted(p.name for p in tmp_path.iterdir())
    assert any(f.endswith(".md") for f in files) and any(f.endswith(".json") for f in files)
    assert "Alpha Manufacturing GmbH" not in "".join(p.read_text() for p in tmp_path.iterdir())

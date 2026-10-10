"""Business process analysis through the add-on's aggregate module: variants with the 80 % line, selectivity,
age profile, growth from the discovery statistics, table-to-object links, usage declared not available; API and
CLI; a table the add-on cannot aggregate is reported, not guessed."""

import json

from sdtf.catalog.store import RecordStore
from sdtf.cli import main as cli_main
from sdtf.discovery import process_analysis as pa
from sdtf.models import SapSystem
from sdtf.runtime import rfc

API = "/api/v1"


def test_analysis_matches_the_record_store(session, slice_result):
    src = session.get(SapSystem, slice_result["source_id"])
    res = pa.analyse(session, src, "ALL", None, top=5, retention_years=3, year=2026)
    store = RecordStore.load(session, src.id, tables=["VBAK", "BKPF", "EKKO", "BSEG", "MSEG"])
    v = next(x for x in res["variants"] if x["table"] == "VBAK" and x["fields"] == ["AUART"])
    assert v["total"] == len(store.rows("VBAK")) and v["variants"] >= 1 and 1 <= v["pareto_variants"] <= v["variants"] and len(v["top"]) <= 5
    assert abs(sum(t["share"] for t in v["top"]) - (v["top"][-1]["cumulative"])) < 0.01 and v["top"][0]["count"] >= v["top"][-1]["count"]
    counts = {}
    for r in store.rows("VBAK"):
        counts[r["AUART"]] = counts.get(r["AUART"], 0) + 1
    assert v["top"][0]["count"] == max(counts.values()) and v["top"][0]["AUART"] in counts
    s = next(x for x in res["selectivity"] if x["table"] == "EKKO" and x["field"] == "LIFNR")
    assert s["distinct"] == len({r["LIFNR"] for r in store.rows("EKKO") if r["LIFNR"]}) and s["rows"] == len(store.rows("EKKO")) and 0 < s["selectivity"] <= 1 and s["top_share"] > 0
    a = next(x for x in res["age"] if x["table"] == "BKPF")
    assert a["rows"] == len(store.rows("BKPF")) and sum(a["by_year"].values()) == a["rows"] and a["horizon_year"] == 2023 and 0 <= a["archivable_share"] <= 1
    assert a["older_than_horizon"] == sum(1 for r in store.rows("BKPF") if int(r["GJAHR"]) < 2023)
    assert res["growth"] and res["growth"][0]["latest_rows"] >= res["growth"][-1]["latest_rows"] and all("by_year" in g for g in res["growth"])
    bkpf = next(g for g in res["growth"] if g["table"] == "BKPF")
    assert "FI.AccountingDocument" in bkpf["objects"] and bkpf["rows"] == a["rows"]
    assert {(l["table"], l["object"]) for l in res["links"]} >= {("VBAK", "SD.SalesOrder"), ("BKPF", "FI.AccountingDocument"), ("EKKO", "MM.PurchaseOrder")}
    assert res["usage"]["status"] == "NOT_AVAILABLE" and "ST03N" in res["usage"]["transactions"] and len(res["matrix"]) == 6
    assert res["transport"] == "SIMULATED_ADDON" and res["rfc_calls"] > 0 and res["errors"] == {}
    # pushdown on one company code: the variants shrink to that company code's rows
    one = pa.analyse(session, src, "R2R", ["5000"], top=3, year=2026)
    b = next(x for x in one["variants"] if x["table"] == "BKPF" and x["fields"] == ["BLART"])
    assert b["pushdown"] and b["total"] == sum(1 for r in store.rows("BKPF") if r["BUKRS"] == "5000") and all(x["table"] in ("BKPF", "BSEG") for x in one["variants"])
    assert "VBAK" not in {x["table"] for x in one["selectivity"]}
    md = pa.report_markdown(one)
    assert "## Process variants (TAANA)" in md and "## Selectivity (DB05)" in md and "BKPF by BLART" in md and "## Usage" in md
    session.expire_all()


def test_unreadable_table_is_reported_not_guessed(session, slice_result, monkeypatch):
    src = session.get(SapSystem, slice_result["source_id"])
    real = rfc.AbapAddonClient.aggregate

    def denied(self, table, preds, group, sums):
        if table == "MSEG":
            raise rfc.RfcError("NOT_AUTHORIZED", "S_TABU_NAM MSEG")
        return real(self, table, preds, group, sums)

    monkeypatch.setattr(rfc.AbapAddonClient, "aggregate", denied)
    res = pa.analyse(session, src, "P2P", None, year=2026)
    assert "MSEG:BWART" in res["errors"] and "NOT_AUTHORIZED" in res["errors"]["MSEG:BWART"] and "MSEG:MATNR" in res["errors"] and "MSEG:MJAHR" in res["errors"]
    assert {x["table"] for x in res["variants"]} == {"EKKO", "RBKP"} and "## Not readable" in pa.report_markdown(res)
    try:
        pa.analyse(session, src, "X2X")
        raise AssertionError("area accepted")
    except ValueError:
        pass
    session.expire_all()


def test_api_and_cli(client, tokens, slice_result, tmp_path, capsys):
    sid = slice_result["source_id"]
    r = client.get(f"{API}/systems/{sid}/process-analysis", params={"area": "O2C", "top": 3, "bukrs": "5000,1000"}, headers=tokens["viewer"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["area"] == "O2C" and d["company_codes"] == ["5000", "1000"] and all(len(v["top"]) <= 3 for v in d["variants"]) and {v["table"] for v in d["variants"]} <= {"VBAK", "LIKP", "VBRK"}
    assert client.get(f"{API}/systems/{sid}/process-analysis", params={"area": "nope"}, headers=tokens["viewer"]).status_code == 422
    r = client.get(f"{API}/systems/{sid}/process-analysis", params={"markdown": "true"}, headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["markdown"].startswith("# Business process analysis")
    assert client.get(f"{API}/systems/nope/process-analysis", headers=tokens["viewer"]).status_code == 404
    out = tmp_path / "bpa.md"
    assert cli_main(["process-analysis", "--system", sid, "--area", "R2R", "--out", str(out)]) == 0
    assert out.read_text().startswith("# Business process analysis") and "BKPF by BLART" in out.read_text()
    capsys.readouterr()
    assert cli_main(["process-analysis", "--system", sid, "--area", "P2P", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["area"] == "P2P"
    assert cli_main(["process-analysis", "--system", "nope"]) == 2

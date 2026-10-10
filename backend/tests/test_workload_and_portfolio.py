"""ST03N transaction-profile import compared with the footprint, and the portfolio KPIs with cross-project
benchmarks; API, CLI and the process analysis carrying the usage."""
from sdtf.discovery.workload import TRANSACTIONS, compare, parse_st03n

API = "/api/v1"
EXPORT = "﻿Transaction\t# Steps\tT Response Time\tØ Time\n" "VA01\t1.234\t987.654\t800,3\n" "VA03\t5.000\t1.000\t200\n" "ME21N\t400\t300\t750\n" "FB01\t250\t100\t400\n" "MIGO\t900\t500\t555,5\n" "ZCUST01\t3.000\t100\t33\n" "SESSION_MANAGER\t9.000\t10\t1\n" "Total\t19.784\t\t\n"


def test_parse_and_compare():
    r = parse_st03n(EXPORT)
    assert r["delimiter"] == "\t" and r["header_row"] == 1 and r["columns"][0] == "Transaction" and not r["errors"]
    rows = {x["tcode"]: x for x in r["rows"]}
    assert rows["VA01"]["steps"] == 1234 and rows["VA01"]["response_ms_total"] == 987654.0 and rows["VA01"]["response_ms_avg"] == 800.3 and "TOTAL" not in rows and len(rows) == 7
    r2 = parse_st03n("Transaction;Steps\nVA01;12\n;5\nVA02;x\n")
    assert [x["steps"] for x in r2["rows"]] == [12] and r2["errors"] == ["2 row(s) skipped: no transaction or no numeric step count"]
    assert parse_st03n("")["errors"] == ["the export holds no rows"]
    assert parse_st03n("a b c\n1 2 3\n")["errors"][0].startswith("no delimiter")
    assert parse_st03n("x,y\n1,2\n")["errors"][0].startswith("no header row")
    assert parse_st03n("Transaction,Users\nVA01,3\n")["errors"][0].startswith("no dialog-step column")
    c = compare(r["rows"], variants=[{"table": "VBAK", "total": 617}, {"table": "EKKO", "total": 100}], growth=[{"table": "BKPF", "rows": 1000}, {"table": "MKPF", "rows": 300}])
    assert c["total_steps"] == 19784 and c["transactions"] == 7 and c["transactions_mapped"] == 5
    by = {d["object"]: d for d in c["by_object"]}
    assert by["SD.SalesOrder"]["steps"] == 6234 and by["SD.SalesOrder"]["write_steps"] == 1234 and by["SD.SalesOrder"]["read_steps"] == 5000 and by["SD.SalesOrder"]["documents_in_db"] == 617 and by["SD.SalesOrder"]["write_steps_per_document"] == 2.0
    assert by["MM.PurchaseOrder"]["write_steps_per_document"] == 4.0 and by["FI.AccountingDocument"]["documents_in_db"] == 1000 and by["MM.MaterialDocument"]["documents_in_db"] == 300
    assert c["by_object"][0]["object"] == "SD.SalesOrder" and c["unmapped_top"][0] == {"tcode": "SESSION_MANAGER", "steps": 9000} and c["mapped_share"] == round((6234 + 400 + 250 + 900) / 19784, 4)
    assert all(len(v) == 4 for v in TRANSACTIONS.values())


def test_workload_api_cli_and_analysis(client, tokens, session, slice_result, capsys, tmp_path):
    from sdtf.models import SapSystem

    sid = slice_result["source_id"]
    assert client.get(f"{API}/systems/{sid}/workload", headers=tokens["viewer"]).json()["status"] == "NOT_AVAILABLE"
    assert client.post(f"{API}/systems/{sid}/workload/import", json={"text": EXPORT, "period": "2026-09"}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/systems/{sid}/workload/import", json={"text": "nothing here"}, headers=tokens["architect"]).status_code == 422
    w = client.post(f"{API}/systems/{sid}/workload/import", json={"text": EXPORT, "period": "2026-09", "source_file": "st03n_sep.txt"}, headers=tokens["architect"])
    assert w.status_code == 200, w.text
    assert w.json()["row_count"] == 7 and w.json()["total_steps"] == 19784 and w.json()["period"] == "2026-09" and "rows" not in w.json()
    u = client.get(f"{API}/systems/{sid}/workload", headers=tokens["viewer"]).json()
    assert u["status"] == "IMPORTED" and u["period"] == "2026-09" and u["by"] == "architect" and u["source_file"] == "st03n_sep.txt"
    by = {d["object"]: d for d in u["by_object"]}
    assert by["SD.SalesOrder"]["documents_in_db"] and by["SD.SalesOrder"]["documents_in_db"] > 0  # from the discovery statistics (growth)
    pa = client.get(f"{API}/systems/{sid}/process-analysis", params={"area": "O2C", "markdown": True}, headers=tokens["viewer"]).json()
    assert pa["usage"]["status"] == "IMPORTED" and pa["usage"]["total_steps"] == 19784 and "## Usage" in pa["markdown"] and "| SD.SalesOrder | VBAK |" in pa["markdown"] and "SESSION_MANAGER (9000)" in pa["markdown"]
    assert any("ST03N transaction-profile export imported" in m["here"] for m in pa["matrix"])
    from sdtf.cli import main

    f = tmp_path / "st03n.txt"
    f.write_text(EXPORT, encoding="utf-8")
    assert main(["workload-import", "--system", sid, "--file", str(f), "--period", "2026-08"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("imported 7 transactions, 19784 dialog steps (2026-08)") and "SD.SalesOrder" in out and "not mapped: SESSION_MANAGER (9000)" in out
    assert main(["workload-import", "--system", "nope", "--file", str(f)]) == 2
    f.write_text("x,y\n1,2\n", encoding="utf-8")
    assert main(["workload-import", "--system", sid, "--file", str(f)]) == 2
    assert client.get(f"{API}/systems/{sid}/workload", headers=tokens["viewer"]).json()["period"] == "2026-08"
    # the shared slice source goes back to "nothing imported" for the tests that expect it
    s = session.get(SapSystem, sid)
    s.meta = {k: v for k, v in (s.meta or {}).items() if k != "workload"}
    session.commit()
    assert client.get(f"{API}/systems/{sid}/workload", headers=tokens["viewer"]).json()["status"] == "NOT_AVAILABLE"


def test_portfolio_kpis(client, tokens, slice_result):
    k = client.get(f"{API}/platform/portfolio/kpis", headers=tokens["viewer"]).json()
    assert k["totals"]["projects"] >= 1 and k["totals"]["completed_runs"] >= 1 and k["totals"]["objects_in_scope"] > 0 and sum(k["totals"]["by_phase"].values()) == k["totals"]["projects"]
    me = next(p for p in k["projects"] if p["id"] == slice_result["project_id"])
    assert me["completed_runs"] >= 1 and me["approved_manifests"] >= 1 and me["approved_rulesets"] >= 1 and me["last_reconciliation"] in ("PASS", "WARN") and me["est_bytes"] > 0
    assert me["phase"] in ("RUN_RECONCILED", "REHEARSING", "CUTOVER_READY") and isinstance(me["open_incidents"], int) and isinstance(me["cleanup_plans"], int)
    b = k["benchmarks"]
    assert b["completed_runs"] >= 1 and b["extraction_rec_s"]["min"] <= b["extraction_rec_s"]["avg"] <= b["extraction_rec_s"]["max"] and b["run_seconds"]["max"] > 0 and "simulators" in b["note"]
    assert me["last_extraction_rec_s"] is None or me["last_extraction_rec_s"] > 0

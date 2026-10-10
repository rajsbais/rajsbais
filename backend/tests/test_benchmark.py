"""Benchmark harness: one measurement at scale 1 carries every step with plausible figures, the report names the
environment and replaces its own section in the document, the CLI writes it."""

import json

from sdtf.benchmark import environment, report_markdown, run_benchmark, write_report
from sdtf.cli import main as cli_main


def test_benchmark_measures_the_slice_and_writes_the_report(session, tmp_path):
    r = run_benchmark(session, scale=1, seed=5, workers=2)
    assert r["simulated"] is True and r["landscape"]["rows"] > 5000 and r["landscape"]["business_objects"] > 500
    for k in ("generate_and_import_s", "discovery_record_store_s", "discovery_add_on_s", "graph_s", "scope_evaluate_s", "run_s", "end_to_end_s"):
        assert r["steps"][k] >= 0, k
    assert r["steps"]["end_to_end_s"] >= r["steps"]["run_s"] and r["discovery_add_on"]["rfc_calls"] > 20 and r["discovery_add_on"]["complete"] is True
    assert r["run"]["status"] == "COMPLETED" and {"EXTRACT", "TRANSFORM", "LOAD", "RECONCILE"} <= set(r["run"]["stages"]) and r["reconciliation"]["checks"] > 100 and r["reconciliation"]["overall"] == "PASS"
    assert r["extraction"]["records_per_second"] is None or r["extraction"]["records_per_second"] > 0
    md = report_markdown([r])
    assert "| 1 |" in md and "simulated run" in md and environment()["python"] in md
    doc = tmp_path / "benchmarks.md"
    doc.write_text("# Benchmarks\n\nintro\n")
    write_report([r], str(doc))
    text = doc.read_text()
    assert text.startswith("# Benchmarks") and text.count("<!-- benchmark:measured:start -->") == 1 and "| 1 |" in text
    write_report([r], str(doc))  # replaces, never duplicates
    assert doc.read_text().count("<!-- benchmark:measured:start -->") == 1 and doc.read_text().count("| 1 |") == 1
    assert json.loads((tmp_path / "benchmarks.json").read_text())["results"][0]["scale"] == 1
    session.expire_all()


def test_benchmark_cli(tmp_path, capsys):
    doc = tmp_path / "b.md"
    assert cli_main(["bench", "--scales", "1", "--seed", "9", "--out", str(doc)]) == 0
    out = capsys.readouterr().out
    assert "scale 1" in out and doc.exists() and "benchmark:measured:start" in doc.read_text()

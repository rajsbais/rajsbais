"""Upload simulation feedback of the migration cockpit: parse the app's message log, match messages to exported
instances, mark rejected instances, classify, and export a retry package."""
import json
import os

import pytest
from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, CockpitFeedback, MigrationRun, TransformationException
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.cockpit_feedback import (
    classify,
    clear_feedback,
    feedback_summary,
    import_feedback,
    parse_feedback,
    rejected_instances,
    sample_feedback,
)
from sdtf.staging import get_backend

API = "/api/v1"


def test_parse_recognises_delimited_json_and_spreadsheetml():
    csv_text = "Migration Object;Instance;Message Type;Message Class;Message Number;Message;Sheet;Field\r\nG/L account;INT/0000113100;E;F5;001;Company code 1000 does not exist;SKB1;BUKRS\r\nG/L account;INT/0000113101;S;;;Instance simulated successfully;;\r\n"
    rows, layout = parse_feedback(csv_text)
    assert layout["format"] == "delimited (;)" and layout["columns"]["key"] == "Instance" and layout["rows"] == 2 and layout["unrecognised"] == []
    assert rows[0] == {"severity": "E", "severity_raw": "E", "message": "Company code 1000 does not exist", "key": "INT/0000113100", "object": "G/L account", "sheet": "SKB1", "field": "BUKRS", "msg_class": "F5", "msg_number": "001", "row": "", "extra": {}}
    rows, layout = parse_feedback("Type\tText\tKey\tBUKRS\tSAKNR\nWarning\tValue converted\t\t1000\t0000113100\n")
    assert layout["format"] == "delimited (tab)" and rows[0]["severity"] == "W" and rows[0]["extra"] == {"BUKRS": "1000", "SAKNR": "0000113100"} and layout["unrecognised"] == ["BUKRS", "SAKNR"]
    rows, layout = parse_feedback('{"messages": [{"severity": "error", "message": "Field KUNRG is mandatory", "instance": "0090000001", "object": "SD.BillingDocument"}]}')
    assert layout["format"] == "json" and rows[0]["severity"] == "E" and rows[0]["object"] == "SD.BillingDocument"
    xml = '<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Messages"><Table><Row><Cell><Data ss:Type="String">Message Type</Data></Cell><Cell><Data ss:Type="String">Message</Data></Cell><Cell><Data ss:Type="String">Instance</Data></Cell></Row><Row><Cell><Data ss:Type="String">E</Data></Cell><Cell><Data ss:Type="String">Record already exists</Data></Cell><Cell><Data ss:Type="String">0090000002</Data></Cell></Row></Table></Worksheet></Workbook>'
    rows, layout = parse_feedback(xml)
    assert layout["format"] == "spreadsheetml" and rows[0]["key"] == "0090000002" and classify(rows[0]["message"]) == "duplicate"
    for bad, msg in (("", "empty"), ("just text\nmore text", "no header row"), ("Instance;Key\n1;2", "no message column"), ("[]", "no rows")):
        with pytest.raises(ValueError, match=msg):
            parse_feedback(bad)
    assert classify("Company code 9999 does not exist") == "configuration_missing" and classify("Field BUKRS is mandatory") == "mandatory_missing" and classify("Invalid date format") == "format_or_value" and classify("No authorization for object") == "authorisation" and classify("something odd") == "other"


def test_import_matches_marks_and_classifies(session, slice_result, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    out = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False)
    assert out["instances"] > 0
    backend = get_backend(session=session)
    gl_rows = [r for r in backend.iter_records(run.id, table="SKB1")]
    bill_rows = [r for r in backend.iter_records(run.id, table="VBRK")]
    gl = gl_rows[0].target_payload
    bill = bill_rows[0].target_payload
    bill2 = bill_rows[1].target_payload
    log = "\n".join([
        "Migration Object,Instance,Message Type,Message,Sheet,Field",
        f"G/L account,{gl['BUKRS']}/{gl['SAKNR']},E,Company code {gl['BUKRS']} does not exist,SKB1,BUKRS",
        f"G/L account,{gl['BUKRS']} {gl['SAKNR'].lstrip('0')},W,Account currency will be defaulted,SKB1,WAERS",  # another separator and no leading zeros: same instance
        f"Historical billing document (custom),{bill['VBELN']},E,Field KUNRG is mandatory and has no value,VBRK,KUNRG",
        f",{bill2['VBELN']},S,Instance simulated successfully,,",  # no object named: key search
        "G/L account,9999/0000000001,E,Record does not exist in staging,SKB1,",
        ",ZZZ,W,Ambiguous key without object,,",
    ])
    summary = import_feedback(session, run.id, log, "sim.csv", "operator")
    assert summary["messages"] == 6 and summary["errors"] == 3 and summary["warnings"] == 2 and summary["matched"] == 4 and summary["unmatched"] == 2
    assert summary["layout"]["format"] == "delimited (,)" and summary["source_files"] == ["sim.csv"]
    assert any("9999/0000000001" in r for r in summary["unmatched_reasons"]) and any("'ZZZ'" in r for r in summary["unmatched_reasons"])
    glo = summary["objects"]["FI.GLAccount"]
    assert glo["with_messages"] == 1 and glo["rejected"] == 1 and glo["errors"] == 1 and glo["warnings"] == 1 and glo["categories"] == {"configuration_missing": 1, "other": 1} and glo["pass_rate"] == 0.0
    bo = summary["objects"]["SD.BillingDocument"]
    assert bo["with_messages"] == 2 and bo["rejected"] == 1 and bo["accepted"] == 1 and bo["pass_rate"] == 0.5 and bo["categories"] == {"mandatory_missing": 1}
    assert summary["rejected_instances"] == 2 and summary["instances_in_log"] == 3 and summary["categories"] == {"configuration_missing": 1, "mandatory_missing": 1, "other": 1}
    # staged rows of rejected instances carry COCKPIT_ERROR with lineage; the accepted one keeps LOADED
    fresh = {r.record_key: r for r in backend.iter_records(run.id, table="SKB1")}
    assert fresh[gl_rows[0].record_key].load_status == "COCKPIT_ERROR" and fresh[gl_rows[0].record_key].lineage[-1]["to"] == "COCKPIT_ERROR"
    ska1 = [r for r in backend.iter_records(run.id, table="SKA1") if r.target_payload.get("SAKNR") == gl["SAKNR"]]
    assert ska1 and all(r.load_status == "LOADED" for r in ska1)  # the chart segment is its own instance (KTOPL|SAKNR): a company-code error does not touch it
    vb = {r.record_key: r for r in backend.iter_records(run.id, table="VBRK")}
    assert vb[bill_rows[0].record_key].load_status == "COCKPIT_ERROR" and vb[bill_rows[1].record_key].load_status == "LOADED"
    items = [r for r in backend.iter_records(run.id, table="VBRP") if r.target_payload.get("VBELN") == bill["VBELN"]]
    assert items and all(r.load_status == "COCKPIT_ERROR" for r in items)
    # exceptions in the COCKPIT stage, errors and warnings, with sheet/field
    ex = session.execute(select(TransformationException).where(TransformationException.run_id == run.id, TransformationException.stage == "COCKPIT")).scalars().all()
    assert sorted((e.severity, e.table_name, e.rule_id) for e in ex) == [("ERROR", "SKB1", "cockpit:configuration_missing"), ("ERROR", "VBRK", "cockpit:mandatory_missing"), ("WARN", "SKB1", "cockpit:other")]
    assert any("[SKB1.BUKRS]" in e.message for e in ex)
    assert rejected_instances(session, run.id) == {("FI.GLAccount", f"{gl['BUKRS']}|{gl['SAKNR']}"), ("SD.BillingDocument", bill["VBELN"])}
    assert session.get(MigrationRun, run.id).report["cockpit_feedback"]["rejected_instances"] == 2
    # retry package: only the rejected instances
    retry = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False, scope="rejected")
    assert retry["scope"] == "rejected" and retry["instances"] == 2 and retry["dir"].endswith("-retry") and retry["zip"].endswith("_retry.zip") and set(retry["objects"]) == {"FI.GLAccount", "SD.BillingDocument"}
    assert retry["objects"]["SD.BillingDocument"]["rows"] == 1 + len(items)
    manifest = json.load(open(os.path.join(retry["dir"], "manifest.json"), encoding="utf-8"))
    assert manifest["scope"] == "rejected" and session.get(MigrationRun, run.id).report["cockpit_retry_export"]["instances"] == 2
    with pytest.raises(ValueError, match="scope"):
        export_cockpit_files(session, run.id, out_dir=str(tmp_path), scope="some")
    # replace vs append; clear resets statuses and exceptions
    import_feedback(session, run.id, "Type,Message,Key\nS,ok,nothing\n", "second.csv", "operator", replace=False)
    assert feedback_summary(session, run.id)["messages"] == 7
    summary2 = import_feedback(session, run.id, "Type,Message,Key\nS,ok,nothing\n", "third.csv", "operator")
    assert summary2["messages"] == 1 and summary2["rejected_instances"] == 0
    assert {r.record_key: r for r in backend.iter_records(run.id, table="VBRK")}[bill_rows[0].record_key].load_status == "COCKPIT_ERROR"  # a replace does not undo marks: clear does
    assert clear_feedback(session, run.id, "operator") == 1
    assert all(r.load_status == "LOADED" for r in backend.iter_records(run.id, table="VBRK")) and "cockpit_feedback" not in session.get(MigrationRun, run.id).report
    assert session.execute(select(TransformationException).where(TransformationException.run_id == run.id, TransformationException.stage == "COCKPIT")).scalars().first() is None
    actions = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == run.id)).scalars().all()]
    assert "COCKPIT_FEEDBACK_IMPORTED" in actions and "COCKPIT_FEEDBACK_CLEARED" in actions


def test_key_columns_and_key_search(session, slice_result, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    backend = get_backend(session=session)
    gl = next(iter(backend.iter_records(run.id, table="SKB1"))).target_payload
    log = f"Message Type\tMessage\tBUKRS\tSAKNR\nE\tField MITKZ is mandatory\t{gl['BUKRS']}\t{gl['SAKNR']}\n"
    s = import_feedback(session, run.id, log, "cols.tsv", "operator")
    assert s["matched"] == 0 and any("no instance key" in r for r in s["unmatched_reasons"])  # key columns need the object to be named
    log = f"Migration Object\tMessage Type\tMessage\tBUKRS\tSAKNR\nFI.GLAccount\tE\tField MITKZ is mandatory\t{gl['BUKRS']}\t{gl['SAKNR']}\n"
    s = import_feedback(session, run.id, log, "cols.tsv", "operator")
    assert s["matched"] == 1 and s["objects"]["FI.GLAccount"]["rejected"] == 1
    clear_feedback(session, run.id, "operator")


def test_sample_feedback_round_trips(session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    text = sample_feedback(session, run.id)
    assert "ILLUSTRATIVE" in text and "Instance simulated successfully" in text
    s = import_feedback(session, run.id, text, "sample.csv", "operator")
    assert s["unmatched"] == 1 and s["matched"] == s["messages"] - 1 and s["rejected_instances"] == len(s["objects"]) and all(o["rejected"] == 1 for o in s["objects"].values())
    assert s["pass_rate"] is not None and 0 < s["pass_rate"] < 1
    clear_feedback(session, run.id, "operator")


def test_cockpit_feedback_api(client, tokens, session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    for f in session.execute(select(CockpitFeedback).where(CockpitFeedback.run_id == run.id)).scalars().all():
        session.delete(f)
    session.commit()
    r = client.get(f"{API}/runs/{run.id}/cockpit-feedback", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["summary"] == {"imported": False} and r.json()["messages"] == []
    r = client.get(f"{API}/runs/{run.id}/cockpit-feedback/sample", headers=tokens["viewer"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    sample = r.text
    assert client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": sample}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": "nonsense"}, headers=tokens["operator"]).status_code == 422
    r = client.post(f"{API}/runs/{run.id}/cockpit-feedback/import", json={"content": sample, "filename": "sim.csv"}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["rejected_instances"] > 0
    r = client.get(f"{API}/runs/{run.id}/cockpit-feedback?severity=E", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["summary"]["imported"] and all(m["severity"] == "E" for m in r.json()["messages"]) and len(r.json()["messages"]) == r.json()["summary"]["errors"]
    r = client.get(f"{API}/runs/{run.id}/cockpit-feedback?unmatched=true", headers=tokens["viewer"])
    assert [m["instance_key"] for m in r.json()["messages"]] == [""] and "ILLUSTRATIVE" in r.json()["messages"][0]["migration_object"]
    r = client.get(f"{API}/runs/{run.id}/exceptions", headers=tokens["viewer"])
    assert any(e["stage"] == "COCKPIT" for e in r.json())
    r = client.get(f"{API}/runs/{run.id}/staged?table=SKB1", headers=tokens["architect"])
    assert any(c["status"] == "COCKPIT_ERROR" for c in r.json()["counts"])
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"scope": "rejected", "use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["scope"] == "rejected" and r.json()["instances"] == client.get(f"{API}/runs/{run.id}/cockpit-feedback", headers=tokens["viewer"]).json()["summary"]["rejected_instances"]
    assert client.post(f"{API}/runs/{run.id}/cockpit-export", json={"scope": "nope"}, headers=tokens["operator"]).status_code == 422
    assert client.delete(f"{API}/runs/{run.id}/cockpit-feedback", headers=tokens["viewer"]).status_code == 403
    r = client.delete(f"{API}/runs/{run.id}/cockpit-feedback", headers=tokens["operator"])
    assert r.status_code == 200 and r.json()["cleared"] > 0
    assert client.get(f"{API}/runs/{run.id}/cockpit-feedback", headers=tokens["viewer"]).json()["summary"] == {"imported": False}
    r = client.get(f"{API}/platform/capabilities", headers=tokens["viewer"])
    assert any("upload simulation feedback import" in c["note"] for c in r.json())


def test_cockpit_feedback_cli(slice_result, session, tmp_path, capsys):
    run_id = slice_result["run_id"]
    session.commit()
    f = tmp_path / "sim.csv"
    assert cli_main(["cockpit-feedback", "sample", "--run", run_id, "--out", str(f)]) == 0 and f.exists()
    capsys.readouterr()
    assert cli_main(["cockpit-feedback", "import", "--run", run_id, "--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert "rejected instances" in out and "unmatched x1" in out
    assert cli_main(["cockpit-feedback", "show", "--run", run_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["imported"] and data["messages"]
    assert cli_main(["cockpit-export", "--run", run_id, "--out", str(tmp_path), "--scope", "rejected", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "rejected"
    bad = tmp_path / "bad.txt"
    bad.write_text("nothing here")
    assert cli_main(["cockpit-feedback", "import", "--run", run_id, "--file", str(bad)]) == 2
    assert cli_main(["cockpit-feedback", "sample", "--run", "nope"]) == 2
    session.expire_all()
    clear_feedback(session, run_id, "cli")
    session.commit()

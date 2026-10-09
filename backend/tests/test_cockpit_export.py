"""Migration cockpit staging-file export: the package carries exactly the rows the LOAD stage routed to the cockpit."""
import csv
import hashlib
import json
import os
import zipfile
from xml.etree import ElementTree as ET

from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, MigrationRun
from sdtf.runtime.cockpit_export import SS, export_cockpit_files
from sdtf.runtime.loaders import EventView, plan_cockpit
from sdtf.staging import get_backend

API = "/api/v1"


def _cockpit_rows_from_staging(session, run_id):
    """Rows whose LOAD lineage says MIGRATION_COCKPIT (LOADED) plus those the cockpit path left UNSUPPORTED/CONFLICT."""
    n = 0
    for r in get_backend(session=session).iter_records(run_id):
        if any(l.get("rule") == "load" and l.get("from") == "MIGRATION_COCKPIT" for l in (r.lineage or [])):
            n += 1
    return n


def test_export_matches_the_load_stage_routing(slice_result, session, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    ld = next(st.metrics for st in run.stages if st.name == "LOAD")
    out = export_cockpit_files(session, run.id, out_dir=str(tmp_path), actor="tester")
    assert out["rows"] == ld["by_method"]["MIGRATION_COCKPIT"] > 0
    assert out["rows"] == _cockpit_rows_from_staging(session, run.id)
    assert set(out["objects"]) >= {"SD.BillingDocument", "SD.SalesOrder", "SD.Delivery"}
    assert all(o["load_status"].get("LOADED", 0) == o["rows"] for o in out["objects"].values())  # everything the simulated cockpit accepted
    assert any("historical A_SalesOrder" in r for r in out["objects"]["SD.SalesOrder"]["reasons"])
    assert "verify" in out["objects"]["SD.BillingDocument"]["migration_object"] or "custom" in out["objects"]["SD.BillingDocument"]["migration_object"]
    # files on disk, checksums in the manifest, zip holds the same bytes
    base = out["dir"]
    manifest = json.load(open(os.path.join(base, "manifest.json"), encoding="utf-8"))
    assert manifest["rows"] == out["rows"] and manifest["disclaimer"].startswith("Transformed staging images")
    assert out["manifest_sha256"] == hashlib.sha256(open(os.path.join(base, "manifest.json"), "rb").read()).hexdigest()
    for rel, meta in manifest["files"].items():
        data = open(os.path.join(base, rel), "rb").read()
        assert hashlib.sha256(data).hexdigest() == meta["sha256"] and len(data) == meta["bytes"]
    with zipfile.ZipFile(out["zip"]) as zf:
        names = set(zf.namelist())
        assert names == set(manifest["files"]) | {"manifest.json"}
        for rel, meta in manifest["files"].items():
            assert hashlib.sha256(zf.read(rel)).hexdigest() == meta["sha256"]
    # CSV: header in catalog order with keys first, one line per row
    with open(os.path.join(base, "SD.BillingDocument", "VBRK.csv"), newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "VBELN" and len(rows) - 1 == manifest["objects"]["SD.BillingDocument"]["rows_by_table"]["VBRK"]
    assert len(rows[1]) == len(rows[0]) and all(r[0] for r in rows[1:])
    # SpreadsheetML: Introduction + Field List + one sheet per table, rows match
    tree = ET.parse(os.path.join(base, "SD.BillingDocument.xml"))
    sheets = {ws.get(f"{{{SS}}}Name"): ws for ws in tree.getroot().findall(f"{{{SS}}}Worksheet")}
    assert set(sheets) == {"Introduction", "Field List", "VBRK", "VBRP"}
    vbrk_rows = sheets["VBRK"].findall(f".//{{{SS}}}Row")
    assert len(vbrk_rows) - 1 == manifest["objects"]["SD.BillingDocument"]["rows_by_table"]["VBRK"]
    header = [d.text for d in vbrk_rows[0].iter(f"{{{SS}}}Data")]
    assert header == rows[0]
    readme = open(os.path.join(base, "README.md"), encoding="utf-8").read()
    assert "Migrate Your Data" in readme and "not generated from the target's migration object templates" in readme.lower().replace("**", "") or "Not generated" in readme
    # audit event and run report
    ev = session.execute(select(AuditEvent).where(AuditEvent.subject_id == run.id, AuditEvent.action == "COCKPIT_EXPORTED")).scalars().all()
    assert ev and ev[-1].details["rows"] == out["rows"] and ev[-1].actor == "tester"
    assert session.get(MigrationRun, run.id).report["cockpit_export"]["zip"] == out["zip"]


def test_export_is_deterministic_and_rewrites_the_package(slice_result, session, tmp_path):
    a = export_cockpit_files(session, slice_result["run_id"], out_dir=str(tmp_path))
    files_a = json.load(open(os.path.join(a["dir"], "manifest.json")))["files"]
    b = export_cockpit_files(session, slice_result["run_id"], out_dir=str(tmp_path))
    files_b = json.load(open(os.path.join(b["dir"], "manifest.json")))["files"]
    csvs = {k: v["sha256"] for k, v in files_a.items() if k.endswith(".csv")}
    assert csvs and csvs == {k: v["sha256"] for k, v in files_b.items() if k.endswith(".csv")}  # timestamps only in the workbooks' Introduction sheet
    assert set(files_a) == set(files_b)
    only_csv = export_cockpit_files(session, slice_result["run_id"], out_dir=str(tmp_path), formats=("csv",))
    assert not any(k.endswith(".xml") for k in json.load(open(os.path.join(only_csv["dir"], "manifest.json")))["files"])


def test_plan_cockpit_routes_like_the_loader():
    hdr = EventView(0, "VBAK", "I", "1", "1", {"VBELN": "1", "GBSTK": "C"}, "SD.SalesOrder", "1", "1")
    item = EventView(0, "VBAP", "I", "1|10", "1|10", {"VBELN": "1", "POSNR": "10"}, "SD.SalesOrder", "1", "1")
    groups, rest = plan_cockpit("SD.SalesOrder", [hdr, item])
    assert rest == [] and len(groups) == 1 and "historical" in groups[0][1] and groups[0][0] == [hdr, item]
    hdr.target_payload["GBSTK"] = "A"
    groups, rest = plan_cockpit("SD.SalesOrder", [hdr, item])
    assert groups == [] and rest == [hdr, item]
    ekbe = EventView(0, "EKBE", "I", "4500|10|1|1|2024|1|1", None, {"EBELN": "4500"}, "MM.PurchaseOrder", "4500", "4500")
    ekko = EventView(0, "EKKO", "I", "4500", None, {"EBELN": "4500", "EBELP": "10"}, "MM.PurchaseOrder", "4500", "4500")
    groups, rest = plan_cockpit("MM.PurchaseOrder", [ekko, ekbe])
    assert rest == [ekko] and groups[0][0] == [ekbe] and "does not expose EKBE" in groups[0][1]
    vbrk = EventView(0, "VBRK", "I", "9", None, {"VBELN": "9"}, "SD.BillingDocument", "9", "9")
    assert plan_cockpit("SD.BillingDocument", [vbrk]) == ([([vbrk], "Historical billing as archive-like history (planned)")], [])
    t001 = EventView(0, "T001", "I", "1000", "1000", {"BUKRS": "1000"}, "CFG.CompanyCode", "1000", "1000")
    assert plan_cockpit("CFG.CompanyCode", [t001]) == ([], [t001])


def test_cockpit_export_api_and_download(client, tokens, session, slice_result):
    from sdtf import config

    run_id = slice_result["run_id"]
    run = session.get(MigrationRun, run_id)
    run.report = {k: v for k, v in (run.report or {}).items() if k != "cockpit_export"}  # earlier tests may have exported
    session.commit()
    r = client.get(f"{API}/runs/{run_id}/cockpit-export", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json() == {"exported": False, "run_id": run_id}
    r = client.post(f"{API}/runs/{run_id}/cockpit-export", headers=tokens["viewer"])
    assert r.status_code == 403
    r = client.post(f"{API}/runs/{run_id}/cockpit-export", json={"formats": ["pdf"]}, headers=tokens["operator"])
    assert r.status_code == 422
    r = client.post(f"{API}/runs/{run_id}/cockpit-export", headers=tokens["operator"])
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["rows"] > 0 and out["dir"] == os.path.join(config.settings.evidence_dir, "cockpit", run_id)
    r = client.get(f"{API}/runs/{run_id}/cockpit-export", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["exported"] is True and r.json()["manifest_sha256"] == out["manifest_sha256"]
    r = client.get(f"{API}/runs/{run_id}/cockpit-export/download", headers=tokens["viewer"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip" and "cockpit_" in r.headers["content-disposition"]
    with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as zf:
        assert "manifest.json" in zf.namelist() and json.loads(zf.read("manifest.json"))["rows"] == out["rows"]
    r = client.get(f"{API}/platform/capabilities", headers=tokens["viewer"])
    assert any(c["area"].startswith("Migration cockpit staging-file export") and c["status"] == "IMPLEMENTED" for c in r.json())


def test_cockpit_export_cli(slice_result, session, tmp_path, capsys):
    session.commit()
    rc = cli_main(["cockpit-export", "--run", slice_result["run_id"], "--out", str(tmp_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] > 0 and os.path.isfile(out["zip"])
    assert cli_main(["cockpit-export", "--run", "nope", "--out", str(tmp_path)]) == 2

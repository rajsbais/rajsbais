"""Migration object lookup per S/4HANA release: catalogue with renames and availability, project registry imported
from the target, resolution in the export, API and CLI."""
import json
import os

import pytest
from sqlalchemy import select

from sdtf.catalog.migration_objects import (
    MIGRATION_OBJECTS,
    NO_STANDARD_OBJECT,
    catalogue,
    lookup,
    lookup_table,
    normalize_release,
)
from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, MigrationObjectEntry, MigrationRun, SapSystem
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.migration_objects import (
    import_entries,
    project_lookup,
    project_registry,
    resolve_for_export,
)

API = "/api/v1"


def test_release_normalisation():
    assert normalize_release("2023") == "2023" and normalize_release("S/4HANA 2023 FPS01") == "2023" and normalize_release("1709") == "1709" and normalize_release("2025") == "2025"
    assert normalize_release("cloud") == "CLOUD" and normalize_release("CE 2402") == "CLOUD" and normalize_release("2402") == "CLOUD"
    assert normalize_release("6.0 EHP8") == "" and normalize_release("") == "" and normalize_release(None) == ""


def test_catalogue_names_follow_releases_and_availability():
    assert lookup("MD.Vendor", "1610")["name"] == "Vendor" and lookup("MD.Vendor", "1709")["name"] == "Supplier" and lookup("MD.Vendor", "2023")["name"] == "Supplier"
    assert lookup("MD.Material", "2020")["name"] == "Material" and lookup("MD.Material", "2021")["name"] == "Product" and lookup("MD.Material", "cloud")["name"] == "Product"
    assert lookup("FI.AccountingDocument", "1610", "BSEG")["name"] == "FI - G/L account balance" and lookup("FI.AccountingDocument", "2023", "BSEG")["name"] == "FI - G/L account balance and open item"
    r = lookup("SD.SalesOrder", "1610")
    assert r["status"] == "none" and "introduced in 1709" in r["note"]
    assert lookup("SD.SalesOrder", "1709")["name"] == "Sales order (open)"
    r = lookup("FI.GLAccount", "2025")
    assert r == {"status": "found", "name": "G/L account", "id": "SIF_GL_ACCOUNT", "release": "2025", "source": "catalogue", "confidence": "documented name, ID unverified", "candidates": [], "note": "chart of accounts and company code segments"}
    assert "release unknown" in lookup("FI.GLAccount", "")["confidence"]
    # several candidates: the staging table decides
    r = lookup("FI.AccountingDocument", "2023")
    assert r["status"] == "candidates" and {c["name"] for c in r["candidates"]} == {"FI - G/L account balance and open item", "FI - Accounts receivable open item", "FI - Accounts payable open item"}
    assert lookup("FI.AccountingDocument", "2023", "BSID")["name"] == "FI - Accounts receivable open item" and lookup("FI.AccountingDocument", "2023", "BSIK")["name"] == "FI - Accounts payable open item"
    assert lookup("MM.InvoiceReceipt", "2023")["name"] == "FI - Accounts payable open item"
    # no standard object, unknown object
    r = lookup("SD.BillingDocument", "2023")
    assert r["status"] == "none" and "custom migration object" in r["note"] and r["confidence"] == "documented"
    assert lookup("CFG.CompanyCode", "2023")["confidence"] == "unknown"
    # catalogue listing and integrity
    cat = catalogue("1610")
    assert next(c for c in cat if c["key"] == "SUPPLIER")["name"] == "Vendor" and next(c for c in cat if c["key"] == "SALES_ORDER_OPEN")["available"] is False
    assert all(c["id_confidence"] == "unverified" for c in cat) and len(MIGRATION_OBJECTS) >= 25
    assert set(NO_STANDARD_OBJECT) & {t for mo in MIGRATION_OBJECTS for t in mo.object_types} == set()
    table = lookup_table("", "2023")
    assert "FI.GLAccount" in table and "SD.BillingDocument" in table and lookup_table("FI.GLAccount", "2023").keys() == {"FI.GLAccount"}


def test_registry_entries_win_for_their_release():
    reg = [{"release": "2023", "name": "G/L Account (custom name)", "id": "SIF_GL_ACCOUNT_CUST", "object_types": ["FI.GLAccount"], "tables": [], "source": "app export"}, {"release": "*", "name": "Billing history (custom)", "id": "Z_BILLING_HIST", "object_types": ["SD.BillingDocument"]}]
    r = lookup("FI.GLAccount", "S/4HANA 2023", None, reg)
    assert (r["status"], r["name"], r["id"], r["source"], r["confidence"]) == ("found", "G/L Account (custom name)", "SIF_GL_ACCOUNT_CUST", "app export", "imported from the target")
    assert lookup("FI.GLAccount", "2022", None, reg)["source"] == "catalogue"  # the entry is for 2023 only
    assert lookup("SD.BillingDocument", "1909", None, reg)["name"] == "Billing history (custom)"  # any release
    assert lookup("FI.AccountingDocument", "2023", "BSID", [{"release": "2023", "name": "AR items", "id": "X", "object_types": ["FI.AccountingDocument"], "tables": ["BSID"]}])["name"] == "AR items"
    assert lookup("FI.AccountingDocument", "2023", "BSIK", [{"release": "2023", "name": "AR items", "id": "X", "object_types": ["FI.AccountingDocument"], "tables": ["BSID"]}])["name"] == "FI - Accounts payable open item"


def test_import_validation_and_project_lookup(session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    pid = run.project_id
    for e in session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == pid)).scalars().all():
        session.delete(e)
    session.flush()
    with pytest.raises(ValueError, match="unrecognised release"):
        import_entries(session, pid, [{"name": "x"}], "architect", release="6.0")
    with pytest.raises(ValueError, match="unknown business object"):
        import_entries(session, pid, [{"name": "x", "object_types": ["XX.Nope"]}], "architect")
    with pytest.raises(ValueError, match="unknown table"):
        import_entries(session, pid, [{"name": "x", "tables": ["NOPE"]}], "architect")
    with pytest.raises(ValueError, match="needs a name"):
        import_entries(session, pid, [{"id": "SIF_X"}], "architect")
    rows = import_entries(session, pid, [{"name": "G/L account", "id": "SIF_GL_ACCOUNT", "object_types": ["FI.GLAccount"], "tables": ["ska1", "SKB1"]}, {"name": "Billing history", "id": "Z_BILL", "release": "*", "object_types": ["SD.BillingDocument"]}], "architect", release="2025", source="app export")
    assert [(e.release, e.name, e.object_id, e.tables) for e in rows] == [("2025", "G/L account", "SIF_GL_ACCOUNT", ["SKA1", "SKB1"]), ("*", "Billing history", "Z_BILL", [])]
    # re-import replaces in place by (release, name) case-insensitively
    rows2 = import_entries(session, pid, [{"name": "g/l ACCOUNT", "id": "SIF_GL_ACCOUNT_V2", "object_types": ["FI.GLAccount"]}], "architect", release="2025")
    assert rows2[0].id == rows[0].id and rows2[0].object_id == "SIF_GL_ACCOUNT_V2" and len(project_registry(session, pid)) == 2
    res = project_lookup(session, pid)
    assert res["release"] == "2025" and res["normalized_release"] == "2025" and res["registry_entries"] == 2
    assert res["objects"]["FI.GLAccount"]["id"] == "SIF_GL_ACCOUNT_V2" and res["objects"]["SD.BillingDocument"]["name"] == "Billing history" and res["objects"]["MD.Vendor"]["name"] == "Supplier"
    assert project_lookup(session, pid, release="1610")["objects"]["MD.Vendor"]["name"] == "Vendor" and project_lookup(session, pid, release="1610")["objects"]["FI.GLAccount"]["source"] == "catalogue"
    rows3 = import_entries(session, pid, [{"name": "Only one", "object_types": ["CO.CostCenter"]}], "architect", replace=True)
    assert len(rows3) == 1 and len(project_registry(session, pid)) == 1 and project_lookup(session, pid)["objects"]["CO.CostCenter"]["confidence"] == "imported name, no ID"
    ev = session.execute(select(AuditEvent).where(AuditEvent.subject_id == pid, AuditEvent.action == "MIGRATION_OBJECTS_IMPORTED")).scalars().all()
    assert ev and ev[-1].details["replace"] is True
    for e in session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == pid)).scalars().all():
        session.delete(e)
    session.flush()


def test_export_resolves_migration_objects_per_table(session, slice_result, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    tgt = session.get(SapSystem, run.target_system_id)
    assert tgt.release == "2025"
    r = resolve_for_export(session, run.project_id, tgt.release, "FI.AccountingDocument", ["BKPF", "BSEG", "BSID", "BSIK"])
    assert r["status"] == "candidates" and r["by_table"]["BSID"]["name"] == "FI - Accounts receivable open item" and r["by_table"]["BSIK"]["name"] == "FI - Accounts payable open item" and r["by_table"]["BKPF"]["name"] == "FI - G/L account balance and open item"
    out = export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False)
    assert out["release"] == "2025"
    manifest = json.load(open(os.path.join(out["dir"], "manifest.json"), encoding="utf-8"))
    gl = manifest["objects"]["FI.GLAccount"]
    assert gl["migration_object"] == "G/L account [SIF_GL_ACCOUNT]" and gl["migration_object_lookup"]["source"] == "catalogue" and gl["migration_object_lookup"]["confidence"] == "documented name, ID unverified"
    bill = manifest["objects"]["SD.BillingDocument"]
    assert bill["migration_object_lookup"]["status"] == "none" and "custom migration object" in bill["migration_object"]
    so = manifest["objects"]["SD.SalesOrder"]
    assert so["migration_object_lookup"]["name"] == "Sales order (open)" and "histories" in so["migration_object_lookup"]["note"]
    readme = open(os.path.join(out["dir"], "README.md"), encoding="utf-8").read()
    assert "| Source / confidence |" in readme and "catalogue / documented name, ID unverified" in readme and "target release (2025)" in readme
    assert out["objects"]["FI.GLAccount"]["migration_object_lookup"]["id"] == "SIF_GL_ACCOUNT"


def test_migration_object_api(client, tokens, session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    pid = run.project_id
    for e in session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == pid)).scalars().all():
        session.delete(e)
    session.commit()
    r = client.get(f"{API}/migration-objects/catalogue?release=1610", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["normalized_release"] == "1610" and "CLOUD" in r.json()["releases"] and next(o for o in r.json()["objects"] if o["key"] == "SUPPLIER")["name"] == "Vendor"
    r = client.get(f"{API}/migration-objects/lookup?object_type=FI.AccountingDocument&release=2023&table=BSIK", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["name"] == "FI - Accounts payable open item"
    r = client.get(f"{API}/projects/{pid}/migration-objects", headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["release"] == "2025" and r.json()["registry"] == [] and r.json()["objects"]["FI.GLAccount"]["source"] == "catalogue"
    body = {"entries": [{"name": "G/L account", "id": "SIF_GL_ACCOUNT", "object_types": ["FI.GLAccount"]}], "release": "2025", "source": "app export"}
    assert client.post(f"{API}/projects/{pid}/migration-objects/import", json=body, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/projects/{pid}/migration-objects/import", json={"entries": [{"name": "x", "object_types": ["XX.No"]}]}, headers=tokens["architect"]).status_code == 422
    r = client.post(f"{API}/projects/{pid}/migration-objects/import", json=body, headers=tokens["architect"])
    assert r.status_code == 201 and r.json()[0]["release"] == "2025" and r.json()[0]["source"] == "app export"
    eid = r.json()[0]["id"]
    r = client.get(f"{API}/projects/{pid}/migration-objects?object_type=FI.GLAccount", headers=tokens["viewer"])
    assert list(r.json()["objects"]) == ["FI.GLAccount"] and r.json()["objects"]["FI.GLAccount"]["confidence"] == "imported from the target" and len(r.json()["registry"]) == 1
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["objects"]["FI.GLAccount"]["migration_object_lookup"]["source"] == "app export"
    assert client.delete(f"{API}/projects/{pid}/migration-objects/{eid}", headers=tokens["operator"]).status_code == 403
    assert client.delete(f"{API}/projects/{pid}/migration-objects/{eid}", headers=tokens["architect"]).status_code == 204
    assert client.delete(f"{API}/projects/{pid}/migration-objects/{eid}", headers=tokens["architect"]).status_code == 404
    r = client.get(f"{API}/platform/capabilities", headers=tokens["viewer"])
    assert any("migration object lookup per target release" in c["note"] for c in r.json())


def test_migration_object_cli(slice_result, session, tmp_path, capsys):
    run = session.get(MigrationRun, slice_result["run_id"])
    session.commit()
    assert cli_main(["migration-objects", "list", "--release", "1709"]) == 0
    out = capsys.readouterr().out
    assert "Supplier" in out and "Sales order (open)" in out and "SIF_GL_ACCOUNT" in out
    assert cli_main(["migration-objects", "list", "--release", "1610", "--object", "SD.SalesOrder"]) == 0
    assert "introduced in 1709" in capsys.readouterr().out
    f = tmp_path / "objects.json"
    f.write_text(json.dumps({"release": "2025", "entries": [{"name": "Product", "id": "SIF_PRODUCT_X", "object_types": ["MD.Material"]}]}))
    assert cli_main(["migration-objects", "import", "--project", run.project_id, "--file", str(f), "--release", "2025"]) == 0
    assert "imported 1 migration object(s)" in capsys.readouterr().out
    assert cli_main(["migration-objects", "list", "--project", run.project_id, "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["objects"]["MD.Material"]["id"] == "SIF_PRODUCT_X" and res["objects"]["MD.Material"]["source"] == "imported from the target's object list"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "x", "tables": ["NOPE"]}]))
    assert cli_main(["migration-objects", "import", "--project", run.project_id, "--file", str(bad)]) == 2
    session.expire_all()
    for e in session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == run.project_id)).scalars().all():
        session.delete(e)
    session.commit()

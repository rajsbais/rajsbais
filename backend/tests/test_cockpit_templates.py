"""Template-driven cockpit export: parse the app's XML templates, map staging columns onto them, fill them."""
import json
import os
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, CockpitTemplate, MigrationRun
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.cockpit_templates import (
    SS,
    auto_map,
    fill_template,
    mapping_report,
    parse_template,
    register_template,
    sample_template,
)

API = "/api/v1"
NS = {"ss": SS}


def _sheet_rows(xml: bytes | str, name: str) -> list[list[str]]:
    root = ET.fromstring(xml)
    ws = next(w for w in root.findall("ss:Worksheet", NS) if w.get(f"{{{SS}}}Name") == name)
    out = []
    for row in ws.findall("ss:Table/ss:Row", NS):
        cells, col = [], 0
        for c in row.findall("ss:Cell", NS):
            idx = c.get(f"{{{SS}}}Index")
            col = int(idx) if idx else col + 1
            while len(cells) < col - 1:
                cells.append("")
            d = c.find("ss:Data", NS)
            cells.append((d.text or "") if d is not None else "")
        out.append(cells)
    return out


def test_sample_template_parses_with_field_list_types_and_header_rows():
    t = parse_template(sample_template("FI.GLAccount"))
    assert "illustrative" in t.migration_object.lower() and t.field_list_sheet == "Field List"
    assert [s.name for s in t.sheets] == ["Chart of Accounts Data", "Company Code Data"]
    sh = t.sheets[0]
    assert sh.header_rows == 8 and [f.name for f in sh.fields] == ["CHRT_ACCTS", "GL_ACCOUNT", "TXT50", "XBILK", "GVTYP", "ACCT_GROUP"]
    f = {x.name: x for x in sh.fields}
    assert f["GL_ACCOUNT"].mandatory and f["GL_ACCOUNT"].key and f["GL_ACCOUNT"].length == 10 and f["GL_ACCOUNT"].type == "CHAR" and f["XBILK"].mandatory is False
    assert f["TXT50"].description == "G/L account long text" and f["TXT50"].column == 3
    with pytest.raises(ValueError, match="no sample template"):
        sample_template("SD.SalesOrder")


def test_parser_handles_sparse_columns_and_templates_without_field_list():
    xml = f'''<?xml version="1.0"?><Workbook xmlns="{SS}" xmlns:ss="{SS}"><Styles><Style ss:ID="s1"><Font ss:Bold="1"/></Style></Styles>
    <Worksheet ss:Name="Readme"><Table><Row><Cell><Data ss:Type="String">Object: Billing history</Data></Cell></Row></Table></Worksheet>
    <Worksheet ss:Name="Header"><Table>
      <Row><Cell ss:StyleID="s1"><Data ss:Type="String">Header</Data></Cell></Row>
      <Row><Cell><Data ss:Type="String">Billing document</Data></Cell><Cell ss:Index="3"><Data ss:Type="String">Net value</Data></Cell></Row>
      <Row><Cell><Data ss:Type="String">VBELN</Data></Cell><Cell ss:Index="3"><Data ss:Type="String">NETWR</Data></Cell><Cell><Data ss:Type="String">FKDAT</Data></Cell></Row>
      <Row><Cell><Data ss:Type="String">sample</Data></Cell></Row>
    </Table></Worksheet></Workbook>'''
    t = parse_template(xml)
    assert t.migration_object == "Billing history" and t.field_list_sheet is None
    (sh,) = t.sheets
    assert sh.header_rows == 3 and sh.data_rows == 1 and [(f.name, f.column) for f in sh.fields] == [("VBELN", 1), ("NETWR", 3), ("FKDAT", 4)]
    m = auto_map(t, "SD.BillingDocument")
    assert m[0].table == "VBRK" and all(f.kind == "direct" for f in m[0].fields)
    out, stats = fill_template(xml, t, m, [{"VBRK": [{"VBELN": "0090000001", "NETWR": 10.5, "FKDAT": "20240131"}]}], header_table="VBRK")
    rows = _sheet_rows(out, "Header")
    assert len(rows) == 4 and rows[3] == ["0090000001", "", "10.5", "20240131"] and stats["Header"]["rows"] == 1  # sample row replaced, column gap kept
    assert b'<Styles><Style ss:ID="s1"><Font ss:Bold="1" /></Style></Styles>' in out and b'ss:StyleID="s1"' in out  # template preserved
    root = ET.fromstring(out)
    assert root.tag == f"{{{SS}}}Workbook"  # elements in the default namespace, attributes keep ss:
    with pytest.raises(ValueError, match="SpreadsheetML"):
        parse_template("<html/>")
    with pytest.raises(ValueError, match="no data sheet"):
        parse_template(f'<Workbook xmlns="{SS}"><Worksheet ss:Name="x" xmlns:ss="{SS}"><Table><Row><Cell><Data ss:Type="String">just text</Data></Cell></Row></Table></Worksheet></Workbook>')


def test_auto_mapping_kinds_overrides_and_report():
    t = parse_template(sample_template("FI.GLAccount"))
    rep = mapping_report(auto_map(t, "FI.GLAccount"))
    coa, cc = rep["sheets"]
    assert coa["table"] == "SKA1" and cc["table"] == "SKB1"
    kinds = {f["field"]: (f["kind"], f["source"]) for f in coa["fields"]}
    assert kinds["CHRT_ACCTS"] == ("alias", "SKA1.KTOPL") and kinds["GL_ACCOUNT"] == ("alias", "SKA1.SAKNR") and kinds["TXT50"] == ("direct", "SKA1.TXT50") and kinds["ACCT_GROUP"] == ("unmapped", None)
    assert rep["mandatory_missing"] == {"Chart of Accounts Data": ["ACCT_GROUP"]} and rep["coverage"] == 0.818 and rep["unmapped_sheets"] == []
    assert cc["unmapped_columns"] == [] and {f["field"]: f["kind"] for f in cc["fields"]}["WAERS"] == "unmapped"
    # overrides: a constant for the account group, blank out a column on purpose, an explicit source
    ov = {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO", "GVTYP": ""}}, "company code data": {"table": "SKB1", "fields": {"WAERS": "=EUR"}}}
    rep2 = mapping_report(auto_map(t, "FI.GLAccount", ov))
    k2 = {f["field"]: f["kind"] for f in rep2["sheets"][0]["fields"]}
    assert k2["ACCT_GROUP"] == "constant" and k2["GVTYP"] == "blank" and rep2["mandatory_missing"] == {} and rep2["coverage"] == 0.909
    assert {f["field"]: f["kind"] for f in rep2["sheets"][1]["fields"]}["WAERS"] == "constant"
    # item sheets take header fields as parent mappings
    b = parse_template(sample_template("SD.BillingDocument"))
    rb = mapping_report(auto_map(b, "SD.BillingDocument"))
    assert {f["field"]: f["kind"] for f in rb["sheets"][1]["fields"]}["KUNRG"] == "parent" and rb["coverage"] == 1.0


def test_fill_types_cells_by_field_list_and_takes_parent_fields():
    xml = sample_template("SD.BillingDocument")
    t = parse_template(xml)
    m = auto_map(t, "SD.BillingDocument", {"Items": {"fields": {"WERKS": "=1000"}}})
    inst = [{"VBRK": [{"VBELN": "0090000001", "FKART": "F2", "VKORG": "1000", "KUNRG": "0000100001", "BUKRS": "1000", "FKDAT": "20240131", "WAERK": "EUR", "NETWR": 1234.5, "GJAHR": "2024"}], "VBRP": [{"VBELN": "0090000001", "POSNR": "000010", "MATNR": "M-1", "WERKS": "2000", "FKIMG": 2, "NETWR": 1234.5}, {"VBELN": "0090000001", "POSNR": "000020", "MATNR": "M-2", "FKIMG": 1.5, "NETWR": 10}]},
            {"VBRK": [{"VBELN": "0090000002", "FKART": "F2", "VKORG": "1000", "KUNRG": "0000100002", "BUKRS": "1000", "FKDAT": "2024-02-29", "WAERK": "EUR", "NETWR": 0, "GJAHR": "2024"}], "VBRP": []}]
    out, stats = fill_template(xml, t, m, inst, header_table="VBRK")
    assert stats == {"Header": {"rows": 2, "length_violations": 0, "table": "VBRK"}, "Items": {"rows": 2, "length_violations": 0, "table": "VBRP"}}
    hdr = _sheet_rows(out, "Header")
    assert hdr[8] == ["0090000001", "F2", "1000", "0000100001", "1000", "2024-01-31T00:00:00.000", "EUR", "1234.5", "2024"]
    assert hdr[9][5] == "2024-02-29T00:00:00.000" and hdr[9][7] == "0"
    items = _sheet_rows(out, "Items")
    assert items[8] == ["0090000001", "000010", "M-1", "1000", "2", "1234.5", "0000100001"] and items[9][6] == "0000100001"  # payer from the header; constant plant
    root = ET.fromstring(out)
    ws = next(w for w in root.findall("ss:Worksheet", NS) if w.get(f"{{{SS}}}Name") == "Header")
    types = [d.get(f"{{{SS}}}Type") for d in ws.findall("ss:Table/ss:Row", NS)[8].iter(f"{{{SS}}}Data")]
    assert types == ["String", "String", "String", "String", "String", "DateTime", "String", "Number", "String"]
    # a value longer than the template field is counted, not truncated
    out2, stats2 = fill_template(xml, t, m, [{"VBRK": [{"VBELN": "0090000001", "FKART": "TOO-LONG"}], "VBRP": []}], header_table="VBRK")
    assert stats2["Header"]["length_violations"] == 1 and _sheet_rows(out2, "Header")[8][1] == "TOO-LONG"


def test_export_fills_registered_templates(slice_result, session, tmp_path):
    run = session.get(MigrationRun, slice_result["run_id"])
    register_template(session, run.project_id, "FI.GLAccount", sample_template("FI.GLAccount"), "gl.xml", "architect", {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}})
    register_template(session, run.project_id, "SD.BillingDocument", sample_template("SD.BillingDocument"), "billing.xml", "architect")
    with pytest.raises(ValueError, match="unknown business object"):
        register_template(session, run.project_id, "XX.Nope", sample_template("FI.GLAccount"), "x.xml", "architect")
    out = export_cockpit_files(session, run.id, out_dir=str(tmp_path), actor="tester")
    assert out["templates"] == 2
    manifest = json.load(open(os.path.join(out["dir"], "manifest.json"), encoding="utf-8"))
    for ot, tables in (("FI.GLAccount", ("SKA1", "SKB1")), ("SD.BillingDocument", ("VBRK", "VBRP"))):
        o = manifest["objects"][ot]
        t = o["template"]
        assert t["file"] == f"{ot}.template.xml" and f"{ot}.template.xml" in manifest["files"] and t["mandatory_missing"] == {}
        assert t["rows"] == o["rows"] and sorted(v["table"] for v in t["sheets"].values()) == sorted(tables)
        assert {v["table"]: v["rows"] for v in t["sheets"].values()} == o["rows_by_table"]
        filled = open(os.path.join(out["dir"], t["file"]), "rb").read()
        parsed = parse_template(filled.decode("utf-8"))
        assert {s.name: s.data_rows for s in parsed.sheets} == {s: v["rows"] for s, v in t["sheets"].items()}
    assert "illustrative" in manifest["objects"]["FI.GLAccount"]["migration_object"].lower()
    assert "SD.SalesOrder.template.xml" not in manifest["files"]  # no template registered for it
    readme = open(os.path.join(out["dir"], "README.md"), encoding="utf-8").read()
    assert "## Filled templates" in readme and "gl.xml" in readme
    # the summary on the run report carries coverage but not the per-sheet detail
    s = session.get(MigrationRun, run.id).report["cockpit_export"]
    assert s["objects"]["FI.GLAccount"]["template"]["coverage"] == 0.909 and "sheets" not in s["objects"]["FI.GLAccount"]["template"]
    assert export_cockpit_files(session, run.id, out_dir=str(tmp_path), use_templates=False)["templates"] == 0


def test_cockpit_template_api(client, tokens, session, slice_result):
    run = session.get(MigrationRun, slice_result["run_id"])
    pid = run.project_id
    for t in session.execute(select(CockpitTemplate).where(CockpitTemplate.project_id == pid)).scalars().all():
        session.delete(t)
    session.commit()
    r = client.get(f"{API}/cockpit-templates/samples", headers=tokens["viewer"])
    assert r.status_code == 200 and "FI.GLAccount" in r.json()["objects"]
    r = client.get(f"{API}/cockpit-templates/samples/FI.GLAccount", headers=tokens["viewer"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml") and "ILLUSTRATIVE" in r.text
    assert client.get(f"{API}/cockpit-templates/samples/SD.SalesOrder", headers=tokens["viewer"]).status_code == 404
    body = {"object_type": "FI.GLAccount", "filename": "gl.xml", "content": r.text}
    assert client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/projects/{pid}/cockpit-templates", json={**body, "content": "<html/>"}, headers=tokens["architect"]).status_code == 422
    r = client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["report"]["mandatory_missing"] == {"Chart of Accounts Data": ["ACCT_GROUP"]} and t["report"]["coverage"] == 0.818 and len(t["sheets"]) == 2
    r = client.put(f"{API}/projects/{pid}/cockpit-templates/{t['id']}/mapping", json={"mapping": {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}}}, headers=tokens["architect"])
    assert r.status_code == 200 and r.json()["report"]["mandatory_missing"] == {}
    r = client.get(f"{API}/projects/{pid}/cockpit-templates", headers=tokens["viewer"])
    assert r.status_code == 200 and [x["object_type"] for x in r.json()] == ["FI.GLAccount"] and r.json()[0]["mapping"]
    r = client.get(f"{API}/projects/{pid}/cockpit-templates/{t['id']}?content=true", headers=tokens["viewer"])
    assert r.status_code == 200 and "ILLUSTRATIVE" in r.json()["content"] and r.json()["structure"]["sheets"][0]["name"] == "Chart of Accounts Data"
    # re-registering replaces in place (same object), and the export uses it
    r = client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["architect"])
    assert r.status_code == 201 and r.json()["id"] == t["id"] and r.json()["report"]["mandatory_missing"] == {}  # mapping kept
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["templates"] == 1 and r.json()["objects"]["FI.GLAccount"]["template"]["coverage"] == 0.909
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["templates"] == 0
    assert client.delete(f"{API}/projects/{pid}/cockpit-templates/{t['id']}", headers=tokens["operator"]).status_code == 403
    assert client.delete(f"{API}/projects/{pid}/cockpit-templates/{t['id']}", headers=tokens["architect"]).status_code == 204
    assert client.get(f"{API}/projects/{pid}/cockpit-templates", headers=tokens["viewer"]).json() == []
    session.expire_all()
    actions = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == pid)).scalars().all()]
    assert "COCKPIT_TEMPLATE_REGISTERED" in actions and "COCKPIT_TEMPLATE_MAPPED" in actions and "COCKPIT_TEMPLATE_DELETED" in actions
    r = client.get(f"{API}/platform/capabilities", headers=tokens["viewer"])
    assert any(c["area"] == "Template-driven cockpit export" and c["status"] == "IMPLEMENTED" for c in r.json())


def test_cockpit_template_cli(slice_result, session, tmp_path, capsys):
    run = session.get(MigrationRun, slice_result["run_id"])
    session.commit()
    f = tmp_path / "gl.xml"
    assert cli_main(["cockpit-template", "sample", "--object", "FI.GLAccount", "--out", str(f)]) == 0 and f.exists()
    assert cli_main(["cockpit-template", "sample", "--object", "SD.SalesOrder"]) == 2
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}}))
    capsys.readouterr()
    assert cli_main(["cockpit-template", "register", "--project", run.project_id, "--object", "FI.GLAccount", "--file", str(f), "--mapping", str(m), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["mandatory_missing"] == {} and out["filename"] == "gl.xml"
    assert cli_main(["cockpit-template", "list", "--project", run.project_id]) == 0
    assert "FI.GLAccount: gl.xml" in capsys.readouterr().out
    assert cli_main(["cockpit-template", "register", "--project", run.project_id, "--object", "XX.Nope", "--file", str(f)]) == 2

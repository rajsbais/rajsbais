"""Template-driven cockpit export: parse the app's XML templates, map staging columns onto them, fill them, and
prove the filled files have the layout SAP's own migration cockpit XML file splitter expects."""
import importlib.util
import json
import os
import sys
import types
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import select

from sdtf.cli import main as cli_main
from sdtf.models import AuditEvent, CockpitTemplate, MigrationRun
from sdtf.runtime.cockpit_export import export_cockpit_files
from sdtf.runtime.cockpit_templates import (
    SS,
    auto_map,
    check_template,
    fill_template,
    mapping_report,
    parse_template,
    register_template,
    sample_template,
)

API = "/api/v1"
NS = {"ss": SS}
VENDORED_SPLITTER = os.path.join(os.path.dirname(__file__), "vendor", "sap_xml_splitter", "splitter.py")


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


def _billing_instances(n: int) -> list[dict]:
    return [{"VBRK": [{"VBELN": f"009000000{i}", "FKART": "F2", "VKORG": "1000", "KUNRG": f"000010000{i}", "BUKRS": "1000", "FKDAT": "20240131", "WAERK": "EUR", "NETWR": 100.5 * i, "GJAHR": "2024"}], "VBRP": [{"VBELN": f"009000000{i}", "POSNR": "000010", "MATNR": "M-1", "WERKS": "1000", "FKIMG": 2, "NETWR": 100.5 * i}, {"VBELN": f"009000000{i}", "POSNR": "000020", "MATNR": "M-2", "FKIMG": 1, "NETWR": 1}]} for i in range(1, n + 1)]


def test_sample_template_has_the_documented_layout():
    """Introduction, Field List with hidden SAP Structure/SAP Field columns, data sheets with hidden rows 4-6, the
    merged key cell in row 7, descriptions with * in row 8, data from row 9."""
    xml = sample_template("FI.GLAccount")
    t = parse_template(xml)
    assert "illustrative" in t.migration_object.lower() and t.field_list_sheet == "Field List" and t.sheet_order[:2] == ["Introduction", "Field List"]
    assert t.field_list_columns["technical_names_in"] == "SAP Field" and t.field_list_columns["columns"]["sapstructure"] == 8 and t.field_list_columns["columns"]["sapfield"] == 9
    assert [s.name for s in t.sheets] == ["Chart of Accounts Data", "Company Code Data"]
    sh = t.sheets[0]
    assert sh.header_rows == 8 and sh.key_columns == 2 and sh.structure == "SKA1" and sh.signals == {"technical_row": 5, "description_row": 8, "key_row": 7, "hidden_rows": [4, 5, 6], "documented_layout": True}
    assert [f.name for f in sh.fields] == ["CHRT_ACCTS", "GL_ACCOUNT", "TXT50", "XBILK", "GVTYP", "ACCT_GROUP"]
    f = {x.name: x for x in sh.fields}
    assert f["GL_ACCOUNT"].mandatory and f["GL_ACCOUNT"].key and f["GL_ACCOUNT"].length == 10 and f["GL_ACCOUNT"].type == "CHAR" and f["XBILK"].mandatory is False and f["XBILK"].key is False
    assert f["TXT50"].description == "G/L Account Long Text" and f["TXT50"].column == 3 and f["ACCT_GROUP"].mandatory
    chk = check_template(xml)
    assert chk["ok"] and chk["documented_layout"] and chk["warnings"] == []
    b = parse_template(sample_template("SD.BillingDocument"))
    assert [(s.key_columns, s.structure) for s in b.sheets] == [(1, "VBRK"), (1, "VBRP")]  # a one-column key is MergeAcross="0", as SAP's splitter reads it
    with pytest.raises(ValueError, match="no sample template"):
        sample_template("SD.SalesOrder")


def test_check_reports_deviations_instead_of_guessing():
    """A template with technical names in row 3, no Field List and no key cell parses, but every assumption is
    named; the parser also tolerates sparse columns and keeps styles."""
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
    assert sh.header_rows == 3 and sh.data_rows == 1 and sh.key_columns == 0 and [(f.name, f.column) for f in sh.fields] == [("VBELN", 1), ("NETWR", 3), ("FKDAT", 4)]
    chk = check_template(xml)
    assert chk["ok"] and not chk["documented_layout"]
    assert any("no Field List" in w for w in chk["warnings"]) and any("worksheet order" in w for w in chk["warnings"]) and any("3 header rows" in w for w in chk["warnings"]) and any("no merged key cell" in w for w in chk["warnings"])
    m = auto_map(t, "SD.BillingDocument")
    assert m[0].table == "VBRK" and m[0].table_by == "score" and all(f.kind == "direct" for f in m[0].fields)
    out, stats = fill_template(xml, t, m, [{"VBRK": [{"VBELN": "0090000001", "NETWR": 10.5, "FKDAT": "20240131"}]}], header_table="VBRK")
    rows = _sheet_rows(out, "Header")
    assert len(rows) == 4 and rows[3] == ["0090000001", "", "10.5", "20240131"] and stats["Header"]["rows"] == 1  # sample row replaced, column gap kept
    assert b'<Styles><Style ss:ID="s1"><Font ss:Bold="1" /></Style></Styles>' in out and b'ss:StyleID="s1"' in out  # template preserved
    assert ET.fromstring(out).tag == f"{{{SS}}}Workbook"  # elements in the default namespace, attributes keep ss:
    assert check_template("<html/>") == {"ok": False, "error": "not a SpreadsheetML 2003 workbook (expected <Workbook xmlns='urn:schemas-microsoft-com:office:spreadsheet'>)", "warnings": [], "sheets": [], "documented_layout": False}
    with pytest.raises(ValueError, match="no data sheet"):
        parse_template(f'<Workbook xmlns="{SS}"><Worksheet ss:Name="x" xmlns:ss="{SS}"><Table><Row><Cell><Data ss:Type="String">just text</Data></Cell></Row></Table></Worksheet></Workbook>')


def test_auto_mapping_kinds_overrides_and_report():
    t = parse_template(sample_template("FI.GLAccount"))
    rep = mapping_report(auto_map(t, "FI.GLAccount"))
    coa, cc = rep["sheets"]
    assert (coa["table"], coa["table_by"]) == ("SKA1", "structure") and (cc["table"], cc["table_by"]) == ("SKB1", "structure")  # SAP Structure names the table
    kinds = {f["field"]: (f["kind"], f["source"]) for f in coa["fields"]}
    assert kinds["CHRT_ACCTS"] == ("alias", "SKA1.KTOPL") and kinds["GL_ACCOUNT"] == ("alias", "SKA1.SAKNR") and kinds["TXT50"] == ("direct", "SKA1.TXT50") and kinds["ACCT_GROUP"] == ("unmapped", None)
    kc = {f["field"]: (f["kind"], f["source"]) for f in cc["fields"]}
    assert kc["CHRT_ACCTS"] == ("related", "SKA1.KTOPL") and kc["COMP_CODE"] == ("alias", "SKB1.BUKRS") and kc["WAERS"] == ("unmapped", None)  # the key repeated from the chart segment
    assert rep["mandatory_missing"] == {"Chart of Accounts Data": ["ACCT_GROUP"]} and rep["coverage"] == 0.833 and rep["unmapped_sheets"] == []
    ov = {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO", "GVTYP": ""}}, "company code data": {"table": "SKB1", "fields": {"WAERS": "=EUR"}}}
    rep2 = mapping_report(auto_map(t, "FI.GLAccount", ov))
    k2 = {f["field"]: f["kind"] for f in rep2["sheets"][0]["fields"]}
    assert k2["ACCT_GROUP"] == "constant" and k2["GVTYP"] == "blank" and rep2["mandatory_missing"] == {} and rep2["coverage"] == 0.917 and rep2["sheets"][1]["table_by"] == "override"
    assert {f["field"]: f["kind"] for f in rep2["sheets"][1]["fields"]}["WAERS"] == "constant"
    b = parse_template(sample_template("SD.BillingDocument"))
    rb = mapping_report(auto_map(b, "SD.BillingDocument"))
    assert {f["field"]: f["kind"] for f in rb["sheets"][1]["fields"]}["KUNRG"] == "parent" and rb["coverage"] == 1.0


def test_fill_types_cells_by_field_list_and_takes_related_fields():
    xml = sample_template("SD.BillingDocument")
    t = parse_template(xml)
    m = auto_map(t, "SD.BillingDocument", {"Items": {"fields": {"WERKS": "=1000"}}})
    inst = _billing_instances(1) + [{"VBRK": [{"VBELN": "0090000009", "FKART": "F2", "VKORG": "1000", "KUNRG": "0000100002", "BUKRS": "1000", "FKDAT": "2024-02-29", "WAERK": "EUR", "NETWR": 0, "GJAHR": "2024"}], "VBRP": []}]
    out, stats = fill_template(xml, t, m, inst, header_table="VBRK")
    assert stats == {"Header": {"rows": 2, "length_violations": 0, "empty_keys": 0, "table": "VBRK"}, "Items": {"rows": 2, "length_violations": 0, "empty_keys": 0, "table": "VBRP"}}
    hdr = _sheet_rows(out, "Header")
    assert hdr[8] == ["0090000001", "F2", "1000", "0000100001", "1000", "2024-01-31T00:00:00.000", "EUR", "100.5", "2024"]
    assert hdr[9][5] == "2024-02-29T00:00:00.000" and hdr[9][7] == "0"
    items = _sheet_rows(out, "Items")
    assert items[8] == ["0090000001", "000010", "M-1", "1000", "2", "100.5", "0000100001"] and items[9][6] == "0000100001"  # payer from the header; constant plant
    ws = next(w for w in ET.fromstring(out).findall("ss:Worksheet", NS) if w.get(f"{{{SS}}}Name") == "Header")
    types = [d.get(f"{{{SS}}}Type") for d in ws.findall("ss:Table/ss:Row", NS)[8].iter(f"{{{SS}}}Data")]
    assert types == ["String", "String", "String", "String", "String", "DateTime", "String", "Number", "String"]
    # line oriented like SAP's files: Row, Cell(+Data), /Row on their own lines; hidden rows and the merged key cell survive
    text = out.decode("utf-8")
    assert '\n<Row ss:Hidden="1">\n' in text and '\n<Cell ss:MergeAcross="0"><Data ss:Type="String">Key</Data></Cell>\n' in text
    for line in text.splitlines():
        assert line.count("<Cell") <= 1 and line.count("<Row") <= 1
        if "<Data" in line:
            assert line.startswith("<Cell") and "</Data></Cell>" in line
    # G/L: the company code sheet takes the chart of accounts from the chart segment of the same instance
    g = sample_template("FI.GLAccount")
    gt = parse_template(g)
    out2, st2 = fill_template(g, gt, auto_map(gt, "FI.GLAccount", {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}}), [{"SKB1": [{"BUKRS": "1000", "SAKNR": "0000113100", "MITKZ": "", "XOPVW": "X"}], "SKA1": [{"KTOPL": "INT", "SAKNR": "0000113100", "TXT50": "Bank", "XBILK": "X", "GVTYP": ""}]}])
    assert _sheet_rows(out2, "Company Code Data")[8] == ["INT", "0000113100", "1000", "", "X"] and _sheet_rows(out2, "Chart of Accounts Data")[8] == ["INT", "0000113100", "Bank", "X", "", "SAKO"] and st2["Company Code Data"]["empty_keys"] == 0
    # a value longer than the template field is counted, not truncated; an empty key is counted
    out3, st3 = fill_template(xml, t, m, [{"VBRK": [{"VBELN": "", "FKART": "TOO-LONG"}], "VBRP": []}], header_table="VBRK")
    assert st3["Header"]["length_violations"] == 1 and st3["Header"]["empty_keys"] == 1 and _sheet_rows(out3, "Header")[8][1] == "TOO-LONG"


def test_filled_file_is_processed_by_sap_xml_file_splitter(tmp_path, monkeypatch, capsys):
    """SAP's own splitter (vendored, Apache-2.0) scans the instances of worksheet 3 after its 8 header rows, reads
    the key span from the merged cell of row 7 and distributes sub-sheet rows by key: our filled file must go
    through it unchanged and come out as valid templates with the instances distributed."""
    monkeypatch.setitem(sys.modules, "progressbar", types.SimpleNamespace(ProgressBar=lambda **kw: types.SimpleNamespace(start=lambda: types.SimpleNamespace(update=lambda *_: None)), Percentage=lambda: None, Bar=lambda *_: None))
    spec = importlib.util.spec_from_file_location("sap_xml_splitter", VENDORED_SPLITTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    xml = sample_template("SD.BillingDocument")
    t = parse_template(xml)
    out, stats = fill_template(xml, t, auto_map(t, "SD.BillingDocument"), _billing_instances(5), header_table="VBRK")
    assert stats["Header"]["rows"] == 5 and stats["Items"]["rows"] == 10
    src = tmp_path / "billing.xml"
    src.write_bytes(out)
    mod.XmlSplitter(str(src), 2).split()
    capsys.readouterr()
    parts = sorted(p.name for p in tmp_path.glob("billing*.xml"))
    assert parts == ["billing.xml", "billing1.xml", "billing2.xml"] and not (tmp_path / "billing_invalid_data.xml").exists()
    seen = {}
    for name in ("billing1.xml", "billing2.xml"):
        pt = parse_template((tmp_path / name).read_text(encoding="utf-8"))
        assert pt.field_list_sheet == "Field List" and all(s.header_rows == 8 and s.key_columns == 1 for s in pt.sheets)
        hdr = _sheet_rows((tmp_path / name).read_bytes(), "Header")[8:]
        items = _sheet_rows((tmp_path / name).read_bytes(), "Items")[8:]
        keys = {r[0] for r in hdr}
        assert len(items) == 2 * len(hdr) and {r[0] for r in items} == keys  # items follow their headers
        seen[name] = keys
    assert seen["billing1.xml"] | seen["billing2.xml"] == {f"009000000{i}" for i in range(1, 6)} and not (seen["billing1.xml"] & seen["billing2.xml"])
    # without the merged key cell the splitter cannot know the key span: the check says so
    chk = check_template(sample_template("FI.GLAccount").replace(' ss:MergeAcross="1"', ""))
    assert any("no merged key cell" in w for w in chk["warnings"])


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
        assert {v["table"]: v["rows"] for v in t["sheets"].values()} == o["rows_by_table"] and all(v["empty_keys"] == 0 for v in t["sheets"].values())
        filled = open(os.path.join(out["dir"], t["file"]), "rb").read()
        parsed = parse_template(filled.decode("utf-8"))
        assert {s.name: s.data_rows for s in parsed.sheets} == {s: v["rows"] for s, v in t["sheets"].items()}
        assert check_template(filled.decode("utf-8"))["documented_layout"]
    assert "illustrative" in manifest["objects"]["FI.GLAccount"]["migration_object"].lower()
    assert "SD.SalesOrder.template.xml" not in manifest["files"]  # no template registered for it
    readme = open(os.path.join(out["dir"], "README.md"), encoding="utf-8").read()
    assert "## Filled templates" in readme and "gl.xml" in readme
    s = session.get(MigrationRun, run.id).report["cockpit_export"]
    assert s["objects"]["FI.GLAccount"]["template"]["coverage"] == 0.917 and "sheets" not in s["objects"]["FI.GLAccount"]["template"]
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
    content = r.text
    assert client.get(f"{API}/cockpit-templates/samples/SD.SalesOrder", headers=tokens["viewer"]).status_code == 404
    r = client.post(f"{API}/cockpit-templates/check", json={"content": content, "object_type": "FI.GLAccount"}, headers=tokens["viewer"])
    assert r.status_code == 200 and r.json()["check"]["documented_layout"] is True and r.json()["mapping"]["coverage"] == 0.833
    r2 = client.post(f"{API}/cockpit-templates/check", json={"content": "<html/>"}, headers=tokens["viewer"])
    assert r2.status_code == 200 and r2.json()["check"]["ok"] is False and r2.json()["mapping"] is None
    body = {"object_type": "FI.GLAccount", "filename": "gl.xml", "content": content}
    assert client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/projects/{pid}/cockpit-templates", json={**body, "content": "<html/>"}, headers=tokens["architect"]).status_code == 422
    r = client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["architect"])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["report"]["mandatory_missing"] == {"Chart of Accounts Data": ["ACCT_GROUP"]} and t["report"]["coverage"] == 0.833 and len(t["sheets"]) == 2 and t["check"] == {"documented_layout": True, "warnings": []}
    assert t["sheets"][0]["key_columns"] == 2 and t["sheets"][0]["structure"] == "SKA1"
    r = client.put(f"{API}/projects/{pid}/cockpit-templates/{t['id']}/mapping", json={"mapping": {"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}}}, headers=tokens["architect"])
    assert r.status_code == 200 and r.json()["report"]["mandatory_missing"] == {}
    r = client.get(f"{API}/projects/{pid}/cockpit-templates", headers=tokens["viewer"])
    assert r.status_code == 200 and [x["object_type"] for x in r.json()] == ["FI.GLAccount"] and r.json()[0]["mapping"]
    r = client.get(f"{API}/projects/{pid}/cockpit-templates/{t['id']}?content=true", headers=tokens["viewer"])
    assert r.status_code == 200 and "ILLUSTRATIVE" in r.json()["content"] and r.json()["structure"]["sheets"][0]["name"] == "Chart of Accounts Data"
    r = client.post(f"{API}/projects/{pid}/cockpit-templates", json=body, headers=tokens["architect"])
    assert r.status_code == 201 and r.json()["id"] == t["id"] and r.json()["report"]["mandatory_missing"] == {}  # replaced in place, mapping kept
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["templates"] == 1 and r.json()["objects"]["FI.GLAccount"]["template"]["coverage"] == 0.917
    r = client.post(f"{API}/runs/{run.id}/cockpit-export", json={"use_templates": False}, headers=tokens["operator"])
    assert r.status_code == 201 and r.json()["templates"] == 0
    assert client.delete(f"{API}/projects/{pid}/cockpit-templates/{t['id']}", headers=tokens["operator"]).status_code == 403
    assert client.delete(f"{API}/projects/{pid}/cockpit-templates/{t['id']}", headers=tokens["architect"]).status_code == 204
    assert client.get(f"{API}/projects/{pid}/cockpit-templates", headers=tokens["viewer"]).json() == []
    session.expire_all()
    actions = [e.action for e in session.execute(select(AuditEvent).where(AuditEvent.subject_id == pid)).scalars().all()]
    assert "COCKPIT_TEMPLATE_REGISTERED" in actions and "COCKPIT_TEMPLATE_MAPPED" in actions and "COCKPIT_TEMPLATE_DELETED" in actions
    r = client.get(f"{API}/platform/capabilities", headers=tokens["viewer"])
    assert any(c["area"] == "Template-driven cockpit export" and c["status"] == "IMPLEMENTED" and "splitter" in c["note"] for c in r.json())


def test_cockpit_template_cli(slice_result, session, tmp_path, capsys):
    run = session.get(MigrationRun, slice_result["run_id"])
    session.commit()
    f = tmp_path / "gl.xml"
    assert cli_main(["cockpit-template", "sample", "--object", "FI.GLAccount", "--out", str(f)]) == 0 and f.exists()
    assert cli_main(["cockpit-template", "sample", "--object", "SD.SalesOrder"]) == 2
    capsys.readouterr()
    assert cli_main(["cockpit-template", "check", "--file", str(f), "--object", "FI.GLAccount"]) == 0
    out = capsys.readouterr().out
    assert "documented layout = True" in out and "2 key column(s)" in out and "mandatory unmapped: {'Chart of Accounts Data': ['ACCT_GROUP']}" in out
    bad = tmp_path / "bad.xml"
    bad.write_text("<html/>")
    assert cli_main(["cockpit-template", "check", "--file", str(bad), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["check"]["ok"] is False
    m = tmp_path / "map.json"
    m.write_text(json.dumps({"Chart of Accounts Data": {"fields": {"ACCT_GROUP": "=SAKO"}}}))
    assert cli_main(["cockpit-template", "register", "--project", run.project_id, "--object", "FI.GLAccount", "--file", str(f), "--mapping", str(m), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["mandatory_missing"] == {} and out["filename"] == "gl.xml" and out["check"]["documented_layout"]
    assert cli_main(["cockpit-template", "list", "--project", run.project_id]) == 0
    assert "FI.GLAccount: gl.xml" in capsys.readouterr().out
    assert cli_main(["cockpit-template", "register", "--project", run.project_id, "--object", "XX.Nope", "--file", str(f)]) == 2

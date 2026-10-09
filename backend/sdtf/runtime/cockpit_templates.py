"""Template-driven migration cockpit export (ADR-0015, fourth step).

The *Migrate Your Data* app provides, per migration object and release, an XML workbook (SpreadsheetML 2003)
with an `Introduction` sheet, a `Field List` sheet (one row per field: sheet, group, technical field name, type,
length, decimals, mandatory, description) and one data sheet per structure whose header rows end with the row of
technical field names; data starts below it. Sheet names, field names and header layout differ between objects
and releases, so this module never hard-codes them: it reads whatever the uploaded template holds.

* `parse_template(xml)`     -> the template's structure (sheets, fields, the technical-name row of each sheet),
                              tolerant of `ss:Index` gaps and of templates without a Field List;
* `auto_map(template, ...)` -> for each data sheet the staging table it corresponds to and, per template field, the
                              staging column that feeds it (same technical name, a known BAPI-style alias, a key of
                              the parent header row, an explicit override, a constant) with coverage and the
                              mandatory fields left unmapped: a report, not a silent guess;
* `fill_template(...)`      -> the template with the staged rows written below each sheet's technical-name row,
                              typed by the Field List (dates as DateTime cells, amounts and quantities as Number
                              cells, everything else as String so SAP keys keep their leading zeros); every other
                              part of the template (styles, header rows, comments) is preserved;
* `sample_template(obj)`    -> an ILLUSTRATIVE template in that layout for development and tests; it is not an SAP
                              file and carries that statement in its Introduction sheet.

Verified only against the illustrative templates: a release's real template must be downloaded from the app and
registered per project; the mapping report says what still has to be decided by hand.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES

SS = "urn:schemas-microsoft-com:office:spreadsheet"
_NS = {"ss": SS}
_KNOWN_NS = {"ss": SS, "o": "urn:schemas-microsoft-com:office:office", "x": "urn:schemas-microsoft-com:office:excel", "html": "http://www.w3.org/TR/REC-html40", "c": "urn:schemas-microsoft-com:office:component:spreadsheet"}
for _p, _u in _KNOWN_NS.items():
    ET.register_namespace(_p, _u)

# BAPI-style field names migration object templates commonly use for DDIC fields (heuristic aliases; the mapping
# report labels them so they can be checked against the template's Field List descriptions).
ALIASES: dict[str, str] = {
    "COMP_CODE": "BUKRS", "COMPANYCODE": "BUKRS", "GL_ACCOUNT": "SAKNR", "GLACCOUNT": "SAKNR", "CHRT_ACCTS": "KTOPL", "CHARTOFACCOUNTS": "KTOPL",
    "CUSTOMER": "KUNNR", "SUPPLIER": "LIFNR", "VENDOR": "LIFNR", "MATERIAL": "MATNR", "PLANT": "WERKS", "SALESORG": "VKORG", "SALES_ORG": "VKORG",
    "DISTR_CHAN": "VTWEG", "DIVISION": "SPART", "DOC_DATE": "BLDAT", "PSTNG_DATE": "BUDAT", "FISC_YEAR": "GJAHR", "FISCALYEAR": "GJAHR", "CURRENCY": "WAERS",
    "DOC_TYPE": "BLART", "DOC_NO": "BELNR", "REF_DOC_NO": "XBLNR", "COSTCENTER": "KOSTL", "PROFIT_CTR": "PRCTR", "ASSET": "ANLN1", "SUBNUMBER": "ANLN2",
    "ASSETCLASS": "ANLKL", "CAP_DATE": "AKTIV", "DESCRIPT": "TXT50", "DESCRIPTION": "TXT50", "TEXT": "TXT50", "BILL_DOC": "VBELN", "BILLINGDOCUMENT": "VBELN",
    "ITM_NUMBER": "POSNR", "ITEM": "POSNR", "QUANTITY": "FKIMG", "NET_VALUE": "NETWR", "NETVALUE": "NETWR", "BILL_TYPE": "FKART", "BILL_DATE": "FKDAT",
    "PAYER": "KUNRG", "PURCH_ORD": "EBELN", "PO_ITEM": "EBELP", "SALES_DOC": "VBELN", "DELIV_NUMB": "VBELN", "MAT_DOC": "MBLNR", "DOC_YEAR": "MJAHR",
    "BAL_SHEET": "XBILK", "PL_STATEMENT": "GVTYP", "RECON_ACCT": "MITKZ", "OPEN_ITEM": "XOPVW", "OPEN_ITEM_MGT": "XOPVW",
}
DATE_TYPES = {"DATS", "DATE", "DATETIME", "D"}
NUMBER_TYPES = {"CURR", "QUAN", "DEC", "FLTP", "INT1", "INT2", "INT4", "INT8", "NUMBER", "AMOUNT", "QUANTITY", "DECIMAL", "P", "F", "I"}
_TRUE = {"X", "YES", "Y", "TRUE", "*", "1"}


@dataclass
class TemplateField:
    name: str  # technical field name as the template's technical-name row holds it
    sheet: str
    column: int  # 1-based column in the data sheet (from the technical-name row)
    group: str = ""
    type: str = ""
    length: int = 0
    decimals: int = 0
    mandatory: bool = False
    key: bool = False
    description: str = ""

    @property
    def kind(self) -> str:
        t = self.type.upper()
        if t in DATE_TYPES:
            return "date"
        if t in NUMBER_TYPES:
            return "number"
        return "string"


@dataclass
class TemplateSheet:
    name: str
    header_rows: int  # rows kept verbatim (up to and including the technical-name row)
    fields: list[TemplateField] = field(default_factory=list)
    data_rows: int = 0  # rows the template already carried below the header (sample rows are discarded on fill)


@dataclass
class Template:
    migration_object: str
    introduction: str
    sheets: list[TemplateSheet]
    field_list_sheet: str | None
    sha256: str

    def sheet(self, name: str) -> TemplateSheet | None:
        return next((s for s in self.sheets if s.name == name), None)

    def structure(self) -> dict:
        return {"migration_object": self.migration_object, "field_list": self.field_list_sheet, "sheets": [{"name": s.name, "header_rows": s.header_rows, "fields": [{"name": f.name, "column": f.column, "group": f.group, "type": f.type, "length": f.length, "decimals": f.decimals, "mandatory": f.mandatory, "key": f.key, "description": f.description} for f in s.fields]} for s in self.sheets], "sha256": self.sha256}


# ------------------------------------------------------------------------------------------------ parsing
def _rows(ws: ET.Element) -> list[list[str]]:
    """Cell texts per row, honouring `ss:Index` (sparse columns)."""
    out = []
    for row in ws.findall("ss:Table/ss:Row", _NS):
        cells: list[str] = []
        col = 0
        for c in row.findall("ss:Cell", _NS):
            idx = c.get(f"{{{SS}}}Index")
            col = int(idx) if idx else col + 1
            while len(cells) < col - 1:
                cells.append("")
            d = c.find("ss:Data", _NS)
            cells.append("".join(d.itertext()).strip() if d is not None else "")
        out.append(cells)
    return out


def _int(v: str) -> int:
    try:
        return int(Decimal(v))
    except (InvalidOperation, ValueError):
        return 0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_template(xml_text: str) -> Template:
    root = ET.fromstring(xml_text)
    if root.tag != f"{{{SS}}}Workbook":
        raise ValueError("not a SpreadsheetML 2003 workbook (expected <Workbook xmlns='urn:schemas-microsoft-com:office:spreadsheet'>)")
    sheets = {ws.get(f"{{{SS}}}Name") or "": _rows(ws) for ws in root.findall("ss:Worksheet", _NS)}
    if not sheets:
        raise ValueError("the workbook has no worksheets")
    intro_name = next((n for n in sheets if _norm(n) in ("introduction", "intro", "readme")), None)
    fl_name = next((n for n in sheets if _norm(n) in ("fieldlist", "fields")), None)
    introduction = "\n".join(" ".join(c for c in r if c) for r in sheets[intro_name] if any(r)) if intro_name else ""
    migration_object = ""
    m = re.search(r"(?:migration object|object)\s*[:\-]\s*(.+)", introduction, re.I)
    if m:
        migration_object = m.group(1).split("\n")[0].strip()[:120]
    # field list: header row detected by its column names
    field_list: dict[str, list[dict]] = {}
    if fl_name:
        rows = sheets[fl_name]
        hdr_i = next((i for i, r in enumerate(rows) if sum(1 for c in r if _norm(c) in ("sheetname", "sheet", "fieldname", "field", "technicalname", "type", "datatype", "length", "mandatory")) >= 3), None)
        if hdr_i is not None:
            cols = {}
            for j, c in enumerate(rows[hdr_i]):
                n = _norm(c)
                for key, names in (("sheet", ("sheetname", "sheet", "structure")), ("group", ("groupname", "group")), ("name", ("fieldname", "field", "technicalname", "technicalfieldname")), ("type", ("type", "datatype")), ("length", ("length", "len")), ("decimals", ("decimalplaces", "decimals", "decimal")), ("mandatory", ("mandatory", "required")), ("key", ("key", "keyfield")), ("description", ("description", "fielddescription", "text"))):
                    if n in names and key not in cols:
                        cols[key] = j
            for r in rows[hdr_i + 1 :]:
                v = {k: (r[j] if j < len(r) else "") for k, j in cols.items()}
                if not v.get("name") or not v.get("sheet"):
                    continue
                field_list.setdefault(v["sheet"], []).append({"name": v["name"].strip(), "group": v.get("group", ""), "type": v.get("type", "").strip().upper(), "length": _int(v.get("length", "")), "decimals": _int(v.get("decimals", "")), "mandatory": v.get("mandatory", "").strip().upper() in _TRUE, "key": v.get("key", "").strip().upper() in _TRUE, "description": v.get("description", "")})
    out: list[TemplateSheet] = []
    for name, rows in sheets.items():
        if name in (intro_name, fl_name):
            continue
        fl = field_list.get(name) or next((v for k, v in field_list.items() if _norm(k) == _norm(name)), [])
        names = {f["name"].upper() for f in fl}
        tech_i = None
        if names:
            for i, r in enumerate(rows):
                vals = [c.upper() for c in r if c]
                if vals and sum(1 for v in vals if v in names) >= max(1, len(vals) // 2):
                    tech_i = i
                    break
        if tech_i is None:  # no field list: the last row whose cells all look like technical names (A-Z0-9_), SAP's layout puts it in row 8
            cands = [i for i, r in enumerate(rows) if r and all(re.fullmatch(r"[A-Z][A-Z0-9_/]*", c) for c in r if c) and sum(1 for c in r if c) >= 1]
            tech_i = next((i for i in cands if i >= 7), cands[-1] if cands else None)
        if tech_i is None:
            continue
        tech = rows[tech_i]
        by_name = {f["name"].upper(): f for f in fl}
        fields = [TemplateField(c.strip(), name, j + 1, **{k: v for k, v in by_name.get(c.strip().upper(), {}).items() if k != "name"}) for j, c in enumerate(tech) if c.strip()]
        out.append(TemplateSheet(name, tech_i + 1, fields, max(0, len(rows) - tech_i - 1)))
    if not out:
        raise ValueError("no data sheet with a technical-name row found (expected a Field List sheet or a header row of technical field names)")
    return Template(migration_object, introduction, out, fl_name, hashlib.sha256(xml_text.encode("utf-8")).hexdigest())


# ------------------------------------------------------------------------------------------------ mapping
@dataclass
class FieldMapping:
    field: str
    column: int
    source: str | None  # "TABLE.FIELD", "=constant" or None
    kind: str  # direct | alias | parent | override | constant | unmapped | blank
    mandatory: bool = False
    type: str = ""
    length: int = 0


@dataclass
class SheetMapping:
    sheet: str
    table: str | None
    fields: list[FieldMapping]
    unmapped_columns: list[str]  # staging columns of the table no template field takes

    def report(self) -> dict:
        mapped = [f for f in self.fields if f.source is not None and f.kind != "blank"]
        return {"sheet": self.sheet, "table": self.table, "fields": [{"field": f.field, "source": f.source, "kind": f.kind, "mandatory": f.mandatory, "type": f.type} for f in self.fields], "coverage": round(len(mapped) / len(self.fields), 3) if self.fields else 0.0, "mapped": len(mapped), "total": len(self.fields), "mandatory_missing": [f.field for f in self.fields if f.mandatory and f.source is None], "unmapped_columns": self.unmapped_columns, "by_kind": {k: sum(1 for f in self.fields if f.kind == k) for k in sorted({f.kind for f in self.fields})}}


def auto_map(template: Template, object_type: str, overrides: dict | None = None) -> list[SheetMapping]:
    """Overrides: `{"<sheet>": {"table": "VBRP", "fields": {"FIELD": "VBRP.NETWR" | "=const" | ""}}}` (an empty
    string leaves the column blank on purpose)."""
    overrides = overrides or {}
    bo = BUSINESS_OBJECTS.get(object_type)
    tables = ([bo.header_table, *bo.item_tables] if bo else []) or sorted(TABLES)
    header = bo.header_table if bo else None
    out: list[SheetMapping] = []
    for sh in template.sheets:
        ov = overrides.get(sh.name) or next((v for k, v in overrides.items() if _norm(k) == _norm(sh.name)), {}) or {}
        ov_fields = {k.upper(): v for k, v in (ov.get("fields") or {}).items()}

        def resolve(fname: str, table: str) -> str | None:
            td = TABLES.get(table)
            if td is None:
                return None
            u = fname.upper()
            if u in td.fields:
                return u
            a = ALIASES.get(u)
            return a if a and a in td.fields else None

        table = ov.get("table")
        if not table:
            scored = sorted(((sum(1 for f in sh.fields if resolve(f.name, t)), -i, t) for i, t in enumerate(tables)), reverse=True)
            table = scored[0][2] if scored and scored[0][0] > 0 else None
        fields: list[FieldMapping] = []
        for f in sh.fields:
            u = f.name.upper()
            if u in ov_fields:
                v = ov_fields[u]
                if v == "" or v is None:
                    fields.append(FieldMapping(f.name, f.column, None, "blank", f.mandatory, f.type, f.length))
                elif str(v).startswith("="):
                    fields.append(FieldMapping(f.name, f.column, str(v), "constant", f.mandatory, f.type, f.length))
                else:
                    fields.append(FieldMapping(f.name, f.column, str(v) if "." in str(v) else f"{table}.{v}", "override", f.mandatory, f.type, f.length))
                continue
            src = resolve(f.name, table) if table else None
            if src is not None:
                fields.append(FieldMapping(f.name, f.column, f"{table}.{src}", "direct" if src == u else "alias", f.mandatory, f.type, f.length))
                continue
            if header and table != header:
                psrc = resolve(f.name, header)
                if psrc is not None:
                    fields.append(FieldMapping(f.name, f.column, f"{header}.{psrc}", "parent", f.mandatory, f.type, f.length))
                    continue
            fields.append(FieldMapping(f.name, f.column, None, "unmapped", f.mandatory, f.type, f.length))
        used = {m.source.split(".", 1)[1] for m in fields if m.source and m.source.startswith(f"{table}.")} if table else set()
        unmapped_cols = [c for c in (TABLES[table].fields if table and table in TABLES else []) if c not in used]
        out.append(SheetMapping(sh.name, table, fields, unmapped_cols))
    return out


def mapping_report(mappings: list[SheetMapping]) -> dict:
    sheets = [m.report() for m in mappings]
    total = sum(s["total"] for s in sheets)
    mapped = sum(s["mapped"] for s in sheets)
    return {"sheets": sheets, "coverage": round(mapped / total, 3) if total else 0.0, "mapped": mapped, "total": total, "mandatory_missing": {s["sheet"]: s["mandatory_missing"] for s in sheets if s["mandatory_missing"]}, "unmapped_sheets": [s["sheet"] for s in sheets if s["table"] is None]}


# ------------------------------------------------------------------------------------------------ filling
def _fmt(value, fm: FieldMapping) -> tuple[str, str, bool]:
    """(cell type, text, length violated) for a staged value by the template field's type."""
    kind = TemplateField(fm.field, "", fm.column, type=fm.type).kind
    if value is None or value == "":
        return "String", "", False
    if isinstance(value, bool):
        value = "X" if value else ""
    s = str(value)
    if kind == "date":
        digits = re.sub(r"\D", "", s)
        if len(digits) == 8:
            try:
                d = date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
                return "DateTime", d.isoformat() + "T00:00:00.000", False
            except ValueError:
                pass
        return "String", s, False
    if kind == "number":
        try:
            n = Decimal(s)
            return "Number", format(n.normalize(), "f") if n != n.to_integral() else str(int(n)), False
        except InvalidOperation:
            return "String", s, False
    return "String", s, bool(fm.length and len(s) > fm.length)


def fill_template(xml_text: str, template: Template, mappings: list[SheetMapping], instances: list[dict[str, list[dict]]], header_table: str | None = None) -> tuple[bytes, dict]:
    """Write the rows of each object instance (`{table: [row, ...]}`) into the template's data sheets. Returns the
    workbook and per-sheet statistics (rows written, length violations). Item sheets take the parent header row's
    fields for `parent` mappings."""
    root = ET.fromstring(xml_text)
    bo_header = header_table
    stats: dict[str, dict] = {}
    by_sheet = {m.sheet: m for m in mappings}
    for ws in root.findall("ss:Worksheet", _NS):
        name = ws.get(f"{{{SS}}}Name") or ""
        sh, m = template.sheet(name), by_sheet.get(name)
        if sh is None or m is None or m.table is None:
            continue
        tbl = ws.find("ss:Table", _NS)
        rows = tbl.findall("ss:Row", _NS)
        for r in rows[sh.header_rows :]:  # drop sample rows the template carried
            tbl.remove(r)
        for attr in (f"{{{SS}}}ExpandedRowCount",):
            if tbl.get(attr) is not None:
                del tbl.attrib[attr]
        written = violations = 0
        for inst in instances:
            parent = (inst.get(bo_header) or [{}])[0] if bo_header else {}
            for row in inst.get(m.table, []):
                el = ET.SubElement(tbl, f"{{{SS}}}Row")
                last = 0
                for fm in sorted(m.fields, key=lambda f: f.column):
                    if fm.source is None:
                        continue
                    if fm.kind == "constant":
                        value = fm.source[1:]
                    else:
                        t, f = fm.source.split(".", 1)
                        value = (parent if (fm.kind == "parent" or t != m.table) else row).get(f)
                        if value is None and t == m.table:
                            value = row.get(f)
                    ctype, text, bad = _fmt(value, fm)
                    violations += bad
                    if text == "":
                        continue
                    cell = ET.SubElement(el, f"{{{SS}}}Cell")
                    if fm.column != last + 1:
                        cell.set(f"{{{SS}}}Index", str(fm.column))
                    last = fm.column
                    d = ET.SubElement(cell, f"{{{SS}}}Data", {f"{{{SS}}}Type": ctype})
                    d.text = text
                written += 1
        stats[name] = {"rows": written, "length_violations": violations, "table": m.table}
    body = ET.tostring(root, encoding="unicode")
    # elements unprefixed in the default namespace, attributes keep their ss: prefix (what Excel and the app expect)
    body = re.sub(r"<(/?)ss:", r"<\1", body)
    body = body.replace("<Workbook ", f'<Workbook xmlns="{SS}" ', 1)
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n'
    return (head + body + "\n").encode("utf-8"), stats


# ------------------------------------------------------------------------------------------------ samples
_SAMPLES: dict[str, tuple[str, list[tuple[str, str, list[tuple[str, str, str, int, bool, str]]]]]] = {
    # object -> (migration object name, [(sheet, group, [(field, description, type, length, mandatory, key)])])
    "FI.GLAccount": ("G/L account (illustrative)", [
        ("Chart of Accounts Data", "General", [("CHRT_ACCTS", "Chart of accounts", "CHAR", 4, True, "X"), ("GL_ACCOUNT", "G/L account number", "CHAR", 10, True, "X"), ("TXT50", "G/L account long text", "CHAR", 50, True, ""), ("XBILK", "Balance sheet account", "CHAR", 1, False, ""), ("GVTYP", "P&L statement account type", "CHAR", 2, False, ""), ("ACCT_GROUP", "Account group", "CHAR", 4, True, "")]),
        ("Company Code Data", "Company code", [("COMP_CODE", "Company code", "CHAR", 4, True, "X"), ("GL_ACCOUNT", "G/L account number", "CHAR", 10, True, "X"), ("MITKZ", "Reconciliation account type", "CHAR", 1, False, ""), ("XOPVW", "Open item management", "CHAR", 1, False, ""), ("WAERS", "Account currency", "CUKY", 5, False, "")]),
    ]),
    "SD.BillingDocument": ("Historical billing document (custom, illustrative)", [
        ("Header", "General", [("VBELN", "Billing document", "CHAR", 10, True, "X"), ("FKART", "Billing type", "CHAR", 4, True, ""), ("VKORG", "Sales organization", "CHAR", 4, True, ""), ("KUNRG", "Payer", "CHAR", 10, True, ""), ("BUKRS", "Company code", "CHAR", 4, True, ""), ("FKDAT", "Billing date", "DATS", 8, True, ""), ("WAERK", "Document currency", "CUKY", 5, True, ""), ("NETWR", "Net value", "CURR", 15, False, ""), ("GJAHR", "Fiscal year", "NUMC", 4, False, "")]),
        ("Items", "Item", [("VBELN", "Billing document", "CHAR", 10, True, "X"), ("POSNR", "Item", "NUMC", 6, True, "X"), ("MATNR", "Material", "CHAR", 40, True, ""), ("WERKS", "Plant", "CHAR", 4, False, ""), ("FKIMG", "Billed quantity", "QUAN", 13, False, ""), ("NETWR", "Net value", "CURR", 15, False, ""), ("KUNRG", "Payer (from header)", "CHAR", 10, False, "")]),
    ]),
}


def _cells(values: list[str], ctype: str = "String") -> str:
    return "".join(f'<Cell><Data ss:Type="{ctype}">{escape(v)}</Data></Cell>' if v != "" else "<Cell/>" for v in values)


def sample_template(object_type: str) -> str:
    """An illustrative template in the layout of the app's XML templates: Introduction, Field List, and per data
    sheet eight header rows (title, description, blank, group, field descriptions, mandatory markers, type/length,
    technical names) with data from row 9. Not an SAP file."""
    if object_type not in _SAMPLES:
        raise ValueError(f"no sample template for {object_type}; samples exist for {', '.join(sorted(_SAMPLES))}")
    mo, sheets = _SAMPLES[object_type]
    intro = ["ILLUSTRATIVE TEMPLATE - not an SAP file. Modelled on the layout of the migration cockpit XML templates (Migrate Your Data app) for development and tests of the template-driven export.", f"Migration object: {mo}", "Download the real template of your release from the app and register it for the project; this sample only demonstrates the mechanics.", "Data starts in row 9 of each data sheet; row 8 holds the technical field names; the Field List sheet describes every field."]
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n<Workbook xmlns="{SS}" xmlns:ss="{SS}">']
    parts.append('<Worksheet ss:Name="Introduction"><Table>' + "".join(f"<Row>{_cells([t])}</Row>" for t in intro) + "</Table></Worksheet>")
    fl = ["<Row>" + _cells(["Sheet Name", "Group Name", "Field Name", "Type", "Length", "Decimal Places", "Mandatory", "Key", "Description"]) + "</Row>"]
    for sheet, group, fields in sheets:
        for f, desc, typ, ln, mand, key in fields:
            fl.append("<Row>" + _cells([sheet, group, f, typ, str(ln), "2" if typ in ("CURR", "QUAN") else "0", "X" if mand else "", key, desc]) + "</Row>")
    parts.append('<Worksheet ss:Name="Field List"><Table>' + "".join(fl) + "</Table></Worksheet>")
    for sheet, group, fields in sheets:
        rows = [_cells([sheet]), _cells([f"{mo}: {sheet}. Enter data from row 9."]), "", _cells([group]), _cells([d for _, d, *_ in fields]), _cells(["*" if m else "" for *_, m, _k in fields]), _cells([f"{t}({n})" for _, _, t, n, _, _ in fields]), _cells([f for f, *_ in fields])]
        parts.append(f'<Worksheet ss:Name="{escape(sheet)}"><Table>' + "".join(f"<Row>{r}</Row>" for r in rows) + "</Table></Worksheet>")
    parts.append("</Workbook>\n")
    return "\n".join(parts)


SAMPLE_OBJECTS = tuple(sorted(_SAMPLES))


# ------------------------------------------------------------------------------------------------ store
def register_template(session, project_id: str, object_type: str, content: str, filename: str, actor: str, mapping: dict | None = None):
    """Parse and store (or replace) the template of one business object for a project. Returns the row."""
    from ..audit.service import record_event
    from ..models import CockpitTemplate

    if object_type not in BUSINESS_OBJECTS:
        raise ValueError(f"unknown business object {object_type}")
    tpl = parse_template(content)
    from sqlalchemy import select

    row = session.execute(select(CockpitTemplate).where(CockpitTemplate.project_id == project_id, CockpitTemplate.object_type == object_type)).scalars().first()
    if row is None:
        row = CockpitTemplate(project_id=project_id, object_type=object_type)
        session.add(row)
    row.content, row.filename, row.sha256, row.structure, row.uploaded_by = content, filename, tpl.sha256, tpl.structure(), actor
    row.migration_object = tpl.migration_object
    if mapping is not None:
        row.mapping = mapping
    session.flush()
    record_event(session, actor, "COCKPIT_TEMPLATE_REGISTERED", "PROJECT", project_id, {"object_type": object_type, "filename": filename, "sha256": tpl.sha256, "sheets": [sh.name for sh in tpl.sheets]})
    return row


def templates_for(session, project_id: str) -> dict:
    from sqlalchemy import select

    from ..models import CockpitTemplate

    return {t.object_type: t for t in session.execute(select(CockpitTemplate).where(CockpitTemplate.project_id == project_id)).scalars().all()}


def template_summary(row) -> dict:
    tpl = parse_template(row.content)
    rep = mapping_report(auto_map(tpl, row.object_type, row.mapping or {}))
    return {"id": row.id, "project_id": row.project_id, "object_type": row.object_type, "migration_object": row.migration_object, "filename": row.filename, "sha256": row.sha256, "uploaded_by": row.uploaded_by, "created_at": row.created_at, "sheets": [{"name": sh.name, "fields": len(sh.fields), "header_rows": sh.header_rows} for sh in tpl.sheets], "mapping": row.mapping or {}, "report": rep}

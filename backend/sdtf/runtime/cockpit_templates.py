"""Template-driven migration cockpit export (ADR-0015, fourth step).

The *Migrate Your Data* app provides, per migration object and release, an XML workbook (Excel XML Spreadsheet
2003). What is known about its layout comes from two public SAP sources, not from a template file (SAP ships the
files only through the app or SAP Note 2470789, both behind a system or S-user login):

* SAP's "Working with the Excel Template" blog (2017, on-premise): worksheet 1 `Introduction`, worksheet 2 `Field
  List` with the columns Sheet Name, Group Name, Field Name, Importance, Type, Length, Decimal Places and the
  hidden columns 8 `SAP Structure` and 9 `SAP Field` (technical names; for ERP sources "often the ERP table and
  field names"); on each data sheet row 4 holds the structure's technical name, row 5 the technical field names and
  row 6 type and length (rows 4-6 hidden), row 8 the field descriptions with `*` marking mandatory fields; data
  starts in row 9; sheets must not be deleted, renamed or reordered.
* SAP's own `s4hana-mc-xml-file-splitter` (SAP-samples, Apache-2.0), which processes real files: worksheet 3 is
  the main sheet and worksheets 4+ the sub sheets; each has exactly 8 header rows and instances from row 9; the
  first cell of row 7 carries `ss:MergeAcross` and its span is the number of key columns (the first columns of
  every sheet); the XML is line oriented (`<Row>`, each `<Cell>` with its `<Data>`, `</Row>` on their own lines).

This module reads whatever the registered template holds rather than assuming that layout, but recognises it:

* `parse_template(xml)`      -> sheets, fields (technical name, description, type, length, mandatory), the header
                               rows to keep, the key columns, with the signals that found them;
* `check_template(xml)`      -> a structured verdict for a downloaded template: what was recognised, what was
                               assumed, what to verify by hand;
* `auto_map(template, ...)`  -> per data sheet the staging table (the `SAP Structure` when it is a catalog table,
                               else the table with most matching fields) and per field the staging column (same
                               technical name, BAPI-style alias, parent key, recorded override, constant), with
                               coverage and the mandatory fields left unmapped;
* `fill_template(...)`       -> the template with rows written below the header rows, typed by the Field List,
                               line oriented so SAP's splitter can process the file; everything else is preserved;
* `sample_template(obj)`     -> an ILLUSTRATIVE template in the documented layout (hidden technical rows, merged key
                               cell, hidden Field List columns). Not an SAP file; its Introduction sheet says so.

Verified: the documented layout (parse, map, fill, re-parse) and interoperability with SAP's splitter on filled
files (tests/test_cockpit_templates.py). Not verified: a template downloaded from a release, which may differ in
wording of the Field List header, in the Importance values or in extra rows. `check_template` reports such
differences instead of guessing silently.
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
from ..catalog.fields import FIELD_DESCRIPTIONS, TEMPLATE_ALIASES
from ..catalog.tables import TABLES

SS = "urn:schemas-microsoft-com:office:spreadsheet"
_NS = {"ss": SS}
_KNOWN_NS = {"ss": SS, "o": "urn:schemas-microsoft-com:office:office", "x": "urn:schemas-microsoft-com:office:excel", "html": "http://www.w3.org/TR/REC-html40", "c": "urn:schemas-microsoft-com:office:component:spreadsheet"}
for _p, _u in _KNOWN_NS.items():
    ET.register_namespace(_p, _u)

ALIASES = TEMPLATE_ALIASES  # global alias catalogue (catalog/fields.py); project aliases learned from Field Lists come first
_DESC_INDEX: dict[str, set[str]] = {}
for _f, _d in FIELD_DESCRIPTIONS.items():
    _DESC_INDEX.setdefault(re.sub(r"[^a-z0-9]", "", _d.lower()), set()).add(_f)
DATE_TYPES = {"DATS", "DATE", "DATETIME", "D"}
NUMBER_TYPES = {"CURR", "QUAN", "DEC", "FLTP", "INT1", "INT2", "INT4", "INT8", "NUMBER", "AMOUNT", "QUANTITY", "DECIMAL", "P", "F", "I"}
_TRUE = {"X", "YES", "Y", "TRUE", "*", "1", "MANDATORY", "REQUIRED", "M"}
DOCUMENTED_HEADER_ROWS = 8  # SAP's splitter: rows 1-8 are header, instances from row 9
_FL_COLUMNS = (
    ("sheet", ("sheetname", "sheet", "structure", "structurename")),
    ("group", ("groupname", "group")),
    ("sapfield", ("sapfield", "technicalname", "technicalfieldname", "fieldtechnicalname")),
    ("sapstructure", ("sapstructure", "structuretechnicalname", "technicalstructure")),
    ("fieldname", ("fieldname", "field", "fielddescription", "description", "text")),
    ("importance", ("importance", "mandatory", "required", "relevance")),
    ("type", ("type", "datatype")),
    ("length", ("length", "len")),
    ("decimals", ("decimalplaces", "decimals", "decimal")),
    ("key", ("key", "keyfield")),
)


@dataclass
class TemplateField:
    name: str  # technical field name
    sheet: str
    column: int  # 1-based column in the data sheet
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
    header_rows: int  # rows kept verbatim; data is written below them
    fields: list[TemplateField] = field(default_factory=list)
    data_rows: int = 0  # rows the template already carried below the header (discarded on fill)
    structure: str = ""  # technical structure name (row 4 / Field List column SAP Structure)
    key_columns: int = 0  # from the merged key cell in row 7 (SAP's splitter reads it the same way)
    signals: dict = field(default_factory=dict)  # which rows were recognised: technical, description, key, hidden


@dataclass
class Template:
    migration_object: str
    introduction: str
    sheets: list[TemplateSheet]
    field_list_sheet: str | None
    sha256: str
    sheet_order: list[str] = field(default_factory=list)
    field_list_columns: dict = field(default_factory=dict)

    def sheet(self, name: str) -> TemplateSheet | None:
        return next((s for s in self.sheets if s.name == name), None)

    def structure(self) -> dict:
        return {"migration_object": self.migration_object, "field_list": self.field_list_sheet, "field_list_columns": self.field_list_columns, "sheets": [{"name": s.name, "header_rows": s.header_rows, "structure": s.structure, "key_columns": s.key_columns, "signals": s.signals, "fields": [{"name": f.name, "column": f.column, "group": f.group, "type": f.type, "length": f.length, "decimals": f.decimals, "mandatory": f.mandatory, "key": f.key, "description": f.description} for f in s.fields]} for s in self.sheets], "sha256": self.sha256}


# ------------------------------------------------------------------------------------------------ parsing
@dataclass
class _Row:
    cells: list[str]
    hidden: bool
    merge_first: int | None  # ss:MergeAcross of the first cell (None when the attribute is absent; 0 is a one-column key)


def _rows(ws: ET.Element) -> list[_Row]:
    """Cell texts per row, honouring `ss:Index` (sparse columns), with the row's hidden flag and the merge span of
    its first cell."""
    out = []
    for row in ws.findall("ss:Table/ss:Row", _NS):
        cells: list[str] = []
        col = 0
        merge_first = None
        for n, c in enumerate(row.findall("ss:Cell", _NS)):
            idx = c.get(f"{{{SS}}}Index")
            col = int(idx) if idx else col + 1
            while len(cells) < col - 1:
                cells.append("")
            d = c.find("ss:Data", _NS)
            cells.append("".join(d.itertext()).strip() if d is not None else "")
            if n == 0 and c.get(f"{{{SS}}}MergeAcross") is not None:
                merge_first = _int(c.get(f"{{{SS}}}MergeAcross"))
        out.append(_Row(cells, row.get(f"{{{SS}}}Hidden") == "1", merge_first))
    return out


def _int(v: str) -> int:
    try:
        return int(Decimal(v))
    except (InvalidOperation, ValueError):
        return 0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _desc(s: str) -> str:
    return _norm(s.replace("*", ""))


_TECH = re.compile(r"[A-Z][A-Z0-9_/]*")


def _field_list(rows: list[_Row]) -> tuple[dict[str, list[dict]], dict]:
    """Field List entries per sheet and the column layout that was recognised."""
    hdr_i = next((i for i, r in enumerate(rows) if sum(1 for c in r.cells if any(_norm(c) in names for _, names in _FL_COLUMNS)) >= 3), None)
    if hdr_i is None:
        return {}, {}
    cols: dict[str, int] = {}
    for j, c in enumerate(rows[hdr_i].cells):
        n = _norm(c)
        for key, names in _FL_COLUMNS:
            if n in names and key not in cols:
                cols[key] = j
                break
    name_col = cols.get("sapfield", cols.get("fieldname"))
    desc_col = cols.get("fieldname") if "sapfield" in cols else None
    out: dict[str, list[dict]] = {}
    for r in rows[hdr_i + 1 :]:
        v = {k: (r.cells[j] if j < len(r.cells) else "") for k, j in cols.items()}
        name = r.cells[name_col] if name_col is not None and name_col < len(r.cells) else ""
        if not name.strip() or not v.get("sheet", "").strip():
            continue
        desc = (r.cells[desc_col] if desc_col is not None and desc_col < len(r.cells) else "").strip()
        out.setdefault(v["sheet"].strip(), []).append({"name": name.strip(), "group": v.get("group", ""), "type": v.get("type", "").strip().upper(), "length": _int(v.get("length", "")), "decimals": _int(v.get("decimals", "")), "mandatory": v.get("importance", "").strip().upper() in _TRUE, "key": v.get("key", "").strip().upper() in _TRUE, "description": desc, "structure": v.get("sapstructure", "").strip()})
    return out, {"header_row": hdr_i + 1, "columns": {k: j + 1 for k, j in cols.items()}, "technical_names_in": "SAP Field" if "sapfield" in cols else "Field Name"}


def parse_template(xml_text: str) -> Template:
    root = ET.fromstring(xml_text)
    if root.tag != f"{{{SS}}}Workbook":
        raise ValueError("not a SpreadsheetML 2003 workbook (expected <Workbook xmlns='urn:schemas-microsoft-com:office:spreadsheet'>)")
    sheets = {ws.get(f"{{{SS}}}Name") or "": _rows(ws) for ws in root.findall("ss:Worksheet", _NS)}
    if not sheets:
        raise ValueError("the workbook has no worksheets")
    order = list(sheets)
    intro_name = next((n for n in sheets if _norm(n) in ("introduction", "intro", "readme")), None)
    fl_name = next((n for n in sheets if _norm(n) in ("fieldlist", "fields")), None)
    introduction = "\n".join(" ".join(c for c in r.cells if c) for r in sheets[intro_name] if any(r.cells)) if intro_name else ""
    migration_object = ""
    m = re.search(r"(?:migration object|object)\s*[:\-]\s*(.+)", introduction, re.I)
    if m:
        migration_object = m.group(1).split("\n")[0].strip()[:120]
    field_list, fl_layout = _field_list(sheets[fl_name]) if fl_name else ({}, {})
    out: list[TemplateSheet] = []
    for name, rows in sheets.items():
        if name in (intro_name, fl_name):
            continue
        fl = field_list.get(name) or next((v for k, v in field_list.items() if _norm(k) == _norm(name)), [])
        names = {f["name"].upper() for f in fl}
        descs = {_desc(f["description"]) for f in fl if f.get("description")}
        tech_i = desc_i = key_i = None
        if names:
            for i, r in enumerate(rows):
                vals = [c.upper() for c in r.cells if c]
                if vals and sum(1 for v in vals if v in names) >= max(1, len(vals) // 2):
                    tech_i = i
                    break
        if tech_i is None:  # no field list: the last header-like row of technical names (A-Z0-9_); SAP's layout has it in row 5
            cands = [i for i, r in enumerate(rows[:DOCUMENTED_HEADER_ROWS]) if r.cells and all(_TECH.fullmatch(c) for c in r.cells if c) and sum(1 for c in r.cells if c) >= 1]
            tech_i = cands[-1] if cands else None
        if tech_i is None:
            continue
        if descs:
            for i, r in enumerate(rows):
                vals = [_desc(c) for c in r.cells if c]
                if i != tech_i and vals and sum(1 for v in vals if v in descs) >= max(1, len(vals) // 2):
                    desc_i = i
                    break
        for i, r in enumerate(rows[:DOCUMENTED_HEADER_ROWS]):  # the key row: first cell merged across the key columns (span 0 = one key, as SAP's splitter reads it)
            if r.merge_first is not None and i > tech_i and (desc_i is None or i < desc_i):
                key_i = i
                break
        header_rows = max(x for x in (tech_i, desc_i, key_i) if x is not None) + 1
        hidden = [i + 1 for i, r in enumerate(rows[:DOCUMENTED_HEADER_ROWS]) if r.hidden]
        if hidden and max(hidden) >= header_rows:  # a header never ends with a hidden technical row
            header_rows = max(hidden) + 1
        tech = rows[tech_i].cells
        by_name = {f["name"].upper(): f for f in fl}
        marks = dict(enumerate(rows[desc_i].cells)) if desc_i is not None else {}
        fields = []
        for j, c in enumerate(tech):
            if not c.strip():
                continue
            spec = {k: v for k, v in by_name.get(c.strip().upper(), {}).items() if k not in ("name", "structure")}
            mark = marks.get(j, "").strip()
            if not spec.get("mandatory") and (mark.endswith("*") or mark.startswith("*")):
                spec["mandatory"] = True
            if not spec.get("description") and mark:
                spec["description"] = mark.replace("*", "").strip()
            fields.append(TemplateField(c.strip(), name, j + 1, **spec))
        key_columns = (rows[key_i].merge_first or 0) + 1 if key_i is not None else 0
        for f in fields[:key_columns]:
            f.key = True
        structure = next((f.get("structure", "") for f in fl if f.get("structure")), "")
        if not structure and tech_i >= 1:
            above = [c for c in rows[tech_i - 1].cells if c]
            if len(above) == 1 and _TECH.fullmatch(above[0]):
                structure = above[0]  # row 4: the structure's technical name above the technical-name row
        documented = header_rows == DOCUMENTED_HEADER_ROWS and tech_i + 1 == 5 and key_i is not None and key_i + 1 == 7 and desc_i is not None and desc_i + 1 == 8
        out.append(TemplateSheet(name, header_rows, fields, max(0, len(rows) - header_rows), structure, key_columns, {"technical_row": tech_i + 1, "description_row": desc_i + 1 if desc_i is not None else None, "key_row": key_i + 1 if key_i is not None else None, "hidden_rows": hidden, "documented_layout": documented}))
    if not out:
        raise ValueError("no data sheet with a technical-name row found (expected a Field List sheet or a header row of technical field names)")
    return Template(migration_object, introduction, out, fl_name, hashlib.sha256(xml_text.encode("utf-8")).hexdigest(), order, fl_layout)


def check_template(xml_text: str) -> dict:
    """Verdict for a downloaded template: what was recognised against the documented layout, what was assumed."""
    try:
        t = parse_template(xml_text)
    except ValueError as e:
        return {"ok": False, "error": str(e), "warnings": [], "sheets": [], "documented_layout": False}
    warnings: list[str] = []
    if t.field_list_sheet is None:
        warnings.append("no Field List sheet: types, lengths and mandatory flags come from the data sheets' header rows only")
    elif t.field_list_columns.get("technical_names_in") != "SAP Field":
        warnings.append("Field List has no 'SAP Field' column: technical names were taken from 'Field Name' (the documented layout has hidden columns 8/9 'SAP Structure'/'SAP Field')")
    if len(t.sheet_order) < 3 or _norm(t.sheet_order[0]) not in ("introduction", "intro", "readme") or _norm(t.sheet_order[1]) not in ("fieldlist", "fields"):
        warnings.append("worksheet order differs from the documented layout (1 Introduction, 2 Field List, 3 main sheet, 4+ sub sheets); SAP's splitter relies on that order")
    sheets = []
    for sh in t.sheets:
        w = []
        if sh.header_rows != DOCUMENTED_HEADER_ROWS:
            w.append(f"{sh.header_rows} header rows recognised (documented layout: 8, data from row 9)")
        if sh.key_columns == 0:
            w.append("no merged key cell in row 7: key columns unknown (SAP's splitter reads the key span from it)")
        if not sh.signals.get("hidden_rows"):
            w.append("no hidden rows 4-6 (structure, technical names, type/length) found")
        if not any(f.mandatory for f in sh.fields):
            w.append("no mandatory fields recognised (Importance column or '*' markers)")
        sheets.append({"name": sh.name, "structure": sh.structure, "fields": len(sh.fields), "header_rows": sh.header_rows, "key_columns": sh.key_columns, "signals": sh.signals, "warnings": w})
        warnings += [f"{sh.name}: {x}" for x in w]
    return {"ok": True, "migration_object": t.migration_object, "sheet_order": t.sheet_order, "field_list": t.field_list_sheet, "field_list_columns": t.field_list_columns, "documented_layout": all(s.signals.get("documented_layout") for s in t.sheets) and not warnings, "sheets": sheets, "warnings": warnings, "sha256": t.sha256}


# ------------------------------------------------------------------------------------------------ mapping
@dataclass
class FieldMapping:
    field: str
    column: int
    source: str | None  # "TABLE.FIELD", "=constant" or None
    kind: str  # direct | project_alias | alias | described | parent | related | override | constant | unmapped | blank
    mandatory: bool = False
    type: str = ""
    length: int = 0


@dataclass
class SheetMapping:
    sheet: str
    table: str | None
    fields: list[FieldMapping]
    unmapped_columns: list[str]  # staging columns of the table no template field takes
    table_by: str = ""  # structure | score | override | none

    def report(self) -> dict:
        mapped = [f for f in self.fields if f.source is not None and f.kind != "blank"]
        return {"sheet": self.sheet, "table": self.table, "table_by": self.table_by, "fields": [{"field": f.field, "source": f.source, "kind": f.kind, "mandatory": f.mandatory, "type": f.type} for f in self.fields], "coverage": round(len(mapped) / len(self.fields), 3) if self.fields else 0.0, "mapped": len(mapped), "total": len(self.fields), "mandatory_missing": [f.field for f in self.fields if f.mandatory and f.source is None], "unmapped_columns": self.unmapped_columns, "by_kind": {k: sum(1 for f in self.fields if f.kind == k) for k in sorted({f.kind for f in self.fields})}}


def _resolve(fname: str, description: str, table: str, aliases: dict | None) -> tuple[str, str] | None:
    """(DDIC field of `table`, kind) for a template field: its own name, a project alias, the global catalogue, or
    a field of the table whose DDIC description equals the template's field description."""
    td = TABLES.get(table)
    if td is None:
        return None
    u = fname.upper()
    if u in td.fields:
        return u, "direct"
    pa = (aliases or {}).get(u)
    if pa and pa[0] in td.fields and (not pa[1] or pa[1] == table):
        return pa[0], "project_alias"
    a = ALIASES.get(u)
    if a and a in td.fields:
        return a, "alias"
    if description:
        cands = [f for f in _DESC_INDEX.get(_norm(description), ()) if f in td.fields]
        if len(cands) == 1:
            return cands[0], "described"
    return None


def auto_map(template: Template, object_type: str, overrides: dict | None = None, aliases: dict | None = None) -> list[SheetMapping]:
    """Overrides: `{"<sheet>": {"table": "VBRP", "fields": {"FIELD": "VBRP.NETWR" | "=const" | ""}}}` (an empty
    string leaves the column blank on purpose). `aliases`: the project's confirmed aliases `{ALIAS: (field, table)}`
    (see `project_aliases`), consulted before the global catalogue."""
    overrides = overrides or {}
    bo = BUSINESS_OBJECTS.get(object_type)
    tables = ([bo.header_table, *bo.item_tables] if bo else []) or sorted(TABLES)
    header = bo.header_table if bo else None
    out: list[SheetMapping] = []
    for sh in template.sheets:
        ov = overrides.get(sh.name) or next((v for k, v in overrides.items() if _norm(k) == _norm(sh.name)), {}) or {}
        ov_fields = {k.upper(): v for k, v in (ov.get("fields") or {}).items()}

        def resolve(f: TemplateField, table: str) -> tuple[str, str] | None:
            return _resolve(f.name, f.description, table, aliases)

        table, table_by = ov.get("table"), "override"
        if not table and sh.structure and sh.structure.upper() in tables:
            table, table_by = sh.structure.upper(), "structure"
        if not table:
            scored = sorted(((sum(1 for f in sh.fields if resolve(f, t)), -i, t) for i, t in enumerate(tables)), reverse=True)
            table, table_by = (scored[0][2], "score") if scored and scored[0][0] > 0 else (None, "none")
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
            res = resolve(f, table) if table else None
            if res is not None:
                fields.append(FieldMapping(f.name, f.column, f"{table}.{res[0]}", res[1], f.mandatory, f.type, f.length))
                continue
            other = next(((t, r[0]) for t in ([header] if header and header != table else []) + [t for t in tables if t not in (table, header)] for r in [resolve(f, t)] if r is not None), None)
            if other is not None:  # the field lives on another table of the same object instance (header keys on item sheets, chart segment next to company code segment)
                fields.append(FieldMapping(f.name, f.column, f"{other[0]}.{other[1]}", "parent" if other[0] == header else "related", f.mandatory, f.type, f.length))
                continue
            fields.append(FieldMapping(f.name, f.column, None, "unmapped", f.mandatory, f.type, f.length))
        used = {m.source.split(".", 1)[1] for m in fields if m.source and m.source.startswith(f"{table}.")} if table else set()
        unmapped_cols = [c for c in (TABLES[table].fields if table and table in TABLES else []) if c not in used]
        out.append(SheetMapping(sh.name, table, fields, unmapped_cols, table_by))
    return out


def _agree(a: str, b: str) -> bool:
    """Two descriptions agree when one contains the other once normalised ('G/L Account Number' ~ 'G/L Account')."""
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x in y or y in x)


def propose_aliases(template: Template, object_type: str, aliases: dict | None = None) -> list[dict]:
    """Aliases a template's Field List suggests for this project: for every field the names alone do not resolve,
    the field of the sheet's table (or of another table of the object) whose DDIC description equals the Field
    List description, and every field the global catalogue resolved only through an alias whose description
    disagrees with the DDIC description (to be checked). Each proposal says what the evidence is."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in auto_map(template, object_type, None, aliases):
        sh = template.sheet(m.sheet)
        if sh is None or m.table is None:
            continue
        by_name = {f.name: f for f in sh.fields}
        for fm in m.fields:
            f = by_name.get(fm.field)
            if f is None:
                continue
            u = f.name.upper()
            if fm.kind == "described":
                t, fld = fm.source.split(".", 1)
                evidence = f"Field List description '{f.description}' equals the DDIC description of {t}.{fld}"
            elif fm.kind == "alias" and f.description and not _agree(f.description, FIELD_DESCRIPTIONS.get(fm.source.split(".", 1)[1], "")):
                t, fld = fm.source.split(".", 1)
                evidence = f"global alias {u} -> {fld}, but the Field List says '{f.description}' and the DDIC description of {fld} is '{FIELD_DESCRIPTIONS.get(fld, '')}': confirm or reject"
            elif fm.kind == "unmapped" and f.description:
                cands = {(t, fld) for t in [m.table] for fld in _DESC_INDEX.get(_norm(f.description), ()) if fld in TABLES[t].fields}
                if not cands:
                    continue
                if len(cands) > 1:
                    evidence = "several fields of the table share this description: " + ", ".join(f"{t}.{fld}" for t, fld in sorted(cands))
                    t, fld = sorted(cands)[0]
                else:
                    (t, fld) = next(iter(cands))
                    evidence = f"Field List description '{f.description}' equals the DDIC description of {t}.{fld}"
            else:
                continue
            if (u, t) in seen:
                continue
            seen.add((u, t))
            out.append({"alias": u, "field": fld, "table": t, "description": f.description, "sheet": m.sheet, "evidence": evidence, "mandatory": f.mandatory})
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


def _line_oriented(body: str) -> str:
    """One `<Worksheet>`, `<Table>`, `<Row>`, each `<Cell>` (with its `<Data>`), `</Row>`, `</Table>` per line: the
    shape SAP's splitter parses (it scans lines for `<Worksheet ss:Name`, `<Row`, `<Cell`, `<Data`, `</Table>`)."""
    return re.sub(r"\s*<(Worksheet |Table|/Table>|Row|/Row>|Cell|/Worksheet>|/Workbook>)", r"\n<\1", body)


def fill_template(xml_text: str, template: Template, mappings: list[SheetMapping], instances: list[dict[str, list[dict]]], header_table: str | None = None) -> tuple[bytes, dict]:
    """Write the rows of each object instance (`{table: [row, ...]}`) into the template's data sheets. Returns the
    workbook and per-sheet statistics (rows written, length violations, empty key cells). `parent` and `related`
    mappings take the value from the instance's row of that other table, or, for segments outside the instance
    (client-level rows such as the chart segment of a G/L account), from the row sharing the key fields."""
    root = ET.fromstring(xml_text)
    stats: dict[str, dict] = {}
    by_sheet = {m.sheet: m for m in mappings}
    pool: dict[str, list[dict]] = {}  # every row per table, for related lookups outside the instance (client-level segments such as SKA1)
    for inst in instances:
        for t, rs in inst.items():
            pool.setdefault(t, []).extend(rs)
    index: dict[tuple, dict[tuple, dict]] = {}

    def related(inst: dict, t: str, row: dict) -> dict:
        if inst.get(t):
            return inst[t][0]
        td = TABLES.get(t)
        shared = tuple(k for k in (td.key_fields if td else ()) if k in row)
        if not shared or t not in pool:
            return {}
        key = (t, shared)
        if key not in index:
            index[key] = {tuple(str(r.get(k)) for k in shared): r for r in reversed(pool[t])}
        return index[key].get(tuple(str(row.get(k)) for k in shared), {})
    for ws in root.findall("ss:Worksheet", _NS):
        name = ws.get(f"{{{SS}}}Name") or ""
        sh, m = template.sheet(name), by_sheet.get(name)
        if sh is None or m is None or m.table is None:
            continue
        tbl = ws.find("ss:Table", _NS)
        rows = tbl.findall("ss:Row", _NS)
        for r in rows[sh.header_rows :]:  # drop sample rows the template carried
            tbl.remove(r)
        if tbl.get(f"{{{SS}}}ExpandedRowCount") is not None:
            del tbl.attrib[f"{{{SS}}}ExpandedRowCount"]
        written = violations = empty_keys = 0
        for inst in instances:
            for row in inst.get(m.table, []):
                el = ET.SubElement(tbl, f"{{{SS}}}Row")
                last = 0
                for fm in sorted(m.fields, key=lambda f: f.column):
                    if fm.source is None:
                        empty_keys += fm.column <= sh.key_columns
                        continue
                    if fm.kind == "constant":
                        value = fm.source[1:]
                    else:
                        t, f = fm.source.split(".", 1)
                        value = row.get(f) if t == m.table else related(inst, t, row).get(f)
                    ctype, text, bad = _fmt(value, fm)
                    violations += bad
                    if text == "":
                        empty_keys += fm.column <= sh.key_columns
                        continue
                    cell = ET.SubElement(el, f"{{{SS}}}Cell")
                    if fm.column != last + 1:
                        cell.set(f"{{{SS}}}Index", str(fm.column))
                    last = fm.column
                    d = ET.SubElement(cell, f"{{{SS}}}Data", {f"{{{SS}}}Type": ctype})
                    d.text = text
                written += 1
        stats[name] = {"rows": written, "length_violations": violations, "empty_keys": empty_keys, "table": m.table}
    body = ET.tostring(root, encoding="unicode")
    # elements unprefixed in the default namespace, attributes keep their ss: prefix (what Excel and the app expect)
    body = re.sub(r"<(/?)ss:", r"<\1", body)
    body = body.replace("<Workbook ", f'<Workbook xmlns="{SS}" ', 1)
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n'
    return (head + _line_oriented(body).lstrip("\n") + "\n").encode("utf-8"), stats


# ------------------------------------------------------------------------------------------------ samples
# object -> (migration object name, key columns, [(sheet, structure, group, [(field, description, type, length, mandatory)])])
# the first sheet is the main sheet; every sheet starts with the key fields
_SAMPLES: dict[str, tuple[str, int, list[tuple[str, str, str, list[tuple[str, str, str, int, bool]]]]]] = {
    "FI.GLAccount": ("G/L account (illustrative)", 2, [
        ("Chart of Accounts Data", "SKA1", "General Data", [("CHRT_ACCTS", "Chart of Accounts", "CHAR", 4, True), ("GL_ACCOUNT", "G/L Account Number", "CHAR", 10, True), ("TXT50", "G/L Account Long Text", "CHAR", 50, True), ("XBILK", "Balance Sheet Account", "CHAR", 1, False), ("GVTYP", "P&L Statement Account Type", "CHAR", 2, False), ("ACCT_GROUP", "Account Group", "CHAR", 4, True)]),
        ("Company Code Data", "SKB1", "Company Code Data", [("CHRT_ACCTS", "Chart of Accounts", "CHAR", 4, True), ("GL_ACCOUNT", "G/L Account Number", "CHAR", 10, True), ("COMP_CODE", "Company Code", "CHAR", 4, True), ("MITKZ", "Reconciliation Account Type", "CHAR", 1, False), ("XOPVW", "Open Item Management", "CHAR", 1, False), ("WAERS", "Account Currency", "CUKY", 5, False)]),
    ]),
    "SD.BillingDocument": ("Historical billing document (custom, illustrative)", 1, [
        ("Header", "VBRK", "General Data", [("VBELN", "Billing Document", "CHAR", 10, True), ("FKART", "Billing Type", "CHAR", 4, True), ("VKORG", "Sales Organization", "CHAR", 4, True), ("KUNRG", "Payer", "CHAR", 10, True), ("BUKRS", "Company Code", "CHAR", 4, True), ("FKDAT", "Billing Date", "DATS", 8, True), ("WAERK", "Document Currency", "CUKY", 5, True), ("NETWR", "Net Value", "CURR", 15, False), ("GJAHR", "Fiscal Year", "NUMC", 4, False)]),
        ("Items", "VBRP", "Item Data", [("VBELN", "Billing Document", "CHAR", 10, True), ("POSNR", "Item", "NUMC", 6, True), ("MATNR", "Material", "CHAR", 40, True), ("WERKS", "Plant", "CHAR", 4, False), ("INVOICED_QTY", "Billed Quantity", "QUAN", 13, False), ("NETWR", "Net Value", "CURR", 15, False), ("KUNRG", "Payer (from header)", "CHAR", 10, False)]),
    ]),
}
SAMPLE_OBJECTS = tuple(sorted(_SAMPLES))


def _cell(v: str, **attrs: str) -> str:
    a = "".join(f' ss:{k}="{escape(str(x))}"' for k, x in attrs.items())
    return f'<Cell{a}><Data ss:Type="String">{escape(v)}</Data></Cell>' if v != "" else f"<Cell{a}/>"


def _row(cells: list[str], hidden: bool = False) -> str:
    return "<Row" + (' ss:Hidden="1"' if hidden else "") + ">\n" + "\n".join(_cell(c) for c in cells) + ("\n" if cells else "") + "</Row>"


def sample_template(object_type: str) -> str:
    """An illustrative template in the documented layout: Introduction; Field List (Sheet Name, Group Name, Field
    Name, Importance, Type, Length, Decimal Places, hidden SAP Structure, hidden SAP Field); per data sheet rows
    1 title, 2 instruction, 3 blank, 4 structure technical name (hidden), 5 technical field names (hidden), 6
    type and length (hidden), 7 group names with the key group merged across the key columns, 8 field descriptions
    with `*` for mandatory fields; data from row 9. Not an SAP file."""
    if object_type not in _SAMPLES:
        raise ValueError(f"no sample template for {object_type}; samples exist for {', '.join(SAMPLE_OBJECTS)}")
    mo, keys, sheets = _SAMPLES[object_type]
    intro = ["ILLUSTRATIVE TEMPLATE - not an SAP file. Built in the layout SAP documents for the migration cockpit XML templates (Migrate Your Data app) so the template-driven export can be developed and tested.", f"Migration object: {mo}", "Download the real template of your release from the app and register it for the project; this sample only demonstrates the mechanics.", "Sheets: Introduction, Field List, then one sheet per structure. Data sheets: rows 1-8 are the header (rows 4-6 hidden: structure, technical field names, type/length; row 7 groups with the key group merged across the key columns; row 8 descriptions, * = mandatory); enter data from row 9."]
    parts = [f'<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n<Workbook xmlns="{SS}" xmlns:ss="{SS}">']
    parts.append('<Worksheet ss:Name="Introduction">\n<Table>\n' + "\n".join(_row([t]) for t in intro) + "\n</Table>\n</Worksheet>")
    fl = [_row(["Field List"]), _row([]), _row(["Sheet Name", "Group Name", "Field Name", "Importance", "Type", "Length", "Decimal Places", "SAP Structure", "SAP Field"])]
    for sheet, structure, group, fields in sheets:
        for f, desc, typ, ln, mand in fields:
            fl.append(_row([sheet, group, desc, "mandatory" if mand else "optional", typ, str(ln), "2" if typ in ("CURR", "QUAN") else "0", structure, f]))
    parts.append('<Worksheet ss:Name="Field List">\n<Table>\n<Column ss:Index="8" ss:Hidden="1"/>\n<Column ss:Hidden="1"/>\n' + "\n".join(fl) + "\n</Table>\n</Worksheet>")
    for sheet, structure, group, fields in sheets:
        n = len(fields)
        r7 = "<Row>\n" + _cell("Key", MergeAcross=str(keys - 1)) + "\n" + (_cell(group, Index=str(keys + 1), MergeAcross=str(n - keys - 1)) + "\n" if n > keys else "") + "</Row>"
        rows = [_row([sheet]), _row([f"{mo}: {sheet}. Enter data from row 9. Fields marked with * are mandatory."]), _row([]), _row([structure], hidden=True), _row([f for f, *_ in fields], hidden=True), _row([f"{t}({ln})" for _, _, t, ln, _ in fields], hidden=True), r7, _row([d + ("*" if m else "") for _, d, _, _, m in fields])]
        parts.append(f'<Worksheet ss:Name="{escape(sheet)}">\n<Table>\n' + "\n".join(rows) + "\n</Table>\n</Worksheet>")
    parts.append("</Workbook>\n")
    return "\n".join(parts)


# ------------------------------------------------------------------------------------------------ store
def register_template(session, project_id: str, object_type: str, content: str, filename: str, actor: str, mapping: dict | None = None):
    """Parse and store (or replace) the template of one business object for a project. Returns the row."""
    from sqlalchemy import select

    from ..audit.service import record_event
    from ..models import CockpitTemplate

    if object_type not in BUSINESS_OBJECTS:
        raise ValueError(f"unknown business object {object_type}")
    tpl = parse_template(content)
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


def project_aliases(session, project_id: str) -> dict[str, tuple[str, str]]:
    """Confirmed aliases of a project: `{ALIAS: (field, table or "")}`."""
    from sqlalchemy import select

    from ..models import CockpitAlias

    return {a.alias.upper(): (a.field, a.table_name) for a in session.execute(select(CockpitAlias).where(CockpitAlias.project_id == project_id, CockpitAlias.status == "CONFIRMED")).scalars().all()}


def store_proposals(session, project_id: str, row, actor: str) -> list:
    """Record the aliases a registered template's Field List proposes (status PROPOSED; existing decisions are
    kept). Returns the proposal rows (new and already known)."""
    from sqlalchemy import select

    from ..models import CockpitAlias

    tpl = parse_template(row.content)
    known = {(a.alias.upper(), a.table_name): a for a in session.execute(select(CockpitAlias).where(CockpitAlias.project_id == project_id)).scalars().all()}
    out = []
    for prop in propose_aliases(tpl, row.object_type, project_aliases(session, project_id)):
        a = known.get((prop["alias"], prop["table"]))
        if a is None:
            a = CockpitAlias(project_id=project_id, alias=prop["alias"], field=prop["field"], table_name=prop["table"], description=prop["description"][:200], evidence=prop["evidence"][:200], object_type=row.object_type, status="PROPOSED", created_by=actor)
            session.add(a)
            known[(prop["alias"], prop["table"])] = a
        out.append(a)
    session.flush()
    return out


def decide_alias(session, alias_row, status: str, actor: str, field: str | None = None) -> None:
    """Confirm or reject a proposal; a confirmation may correct the target field."""
    from ..audit.service import record_event

    if status not in ("CONFIRMED", "REJECTED"):
        raise ValueError("status must be CONFIRMED or REJECTED")
    if field:
        td = TABLES.get(alias_row.table_name) if alias_row.table_name else None
        if td is not None and field.upper() not in td.fields:
            raise ValueError(f"{field} is not a field of {alias_row.table_name}")
        if td is None and field.upper() not in FIELD_DESCRIPTIONS:
            raise ValueError(f"{field} is not a field of the table catalog")
        alias_row.field = field.upper()
    alias_row.status = status
    alias_row.decided_by = actor
    session.flush()
    record_event(session, actor, f"COCKPIT_ALIAS_{status}", "PROJECT", alias_row.project_id, {"alias": alias_row.alias, "field": alias_row.field, "table": alias_row.table_name})


def alias_out(a) -> dict:
    return {"id": a.id, "project_id": a.project_id, "alias": a.alias, "field": a.field, "table": a.table_name, "description": a.description, "evidence": a.evidence, "object_type": a.object_type, "status": a.status, "created_by": a.created_by, "decided_by": a.decided_by, "created_at": a.created_at, "ddic_description": FIELD_DESCRIPTIONS.get(a.field, "")}


def template_summary(row, aliases: dict | None = None, proposals: list | None = None) -> dict:
    tpl = parse_template(row.content)
    rep = mapping_report(auto_map(tpl, row.object_type, row.mapping or {}, aliases))
    chk = check_template(row.content)
    props = proposals if proposals is not None else propose_aliases(tpl, row.object_type, aliases)
    return {"id": row.id, "project_id": row.project_id, "object_type": row.object_type, "migration_object": row.migration_object, "filename": row.filename, "sha256": row.sha256, "uploaded_by": row.uploaded_by, "created_at": row.created_at, "sheets": [{"name": sh.name, "fields": len(sh.fields), "header_rows": sh.header_rows, "key_columns": sh.key_columns, "structure": sh.structure} for sh in tpl.sheets], "mapping": row.mapping or {}, "report": rep, "check": {"documented_layout": chk.get("documented_layout"), "warnings": chk.get("warnings", [])}, "alias_proposals": props}

# Validation of the template-driven cockpit export against the real template layout

**Date:** 2026-10-09 · **Scope:** `backend/sdtf/runtime/cockpit_templates.py` (parser, automatic mapping, filler),
`sdtf cockpit-template check`, `POST /cockpit-templates/check`.

## What a "real release template" is and why none is in this repository

The *Migrate Your Data* app of an SAP S/4HANA system generates, per migration object and release, an *Excel XML
Spreadsheet 2003* file. SAP distributes these files only through the app (download per object) and, for the cloud
edition, through SAP Note 2470789 (sample templates), both behind a system logon or an S-user. They are SAP
property and are not published in code repositories. This environment has no S/4HANA system and its network
policy blocks `help.sap.com`, `community.sap.com`, `blogs.sap.com` and the web archives, so **no template file
downloaded from a release could be obtained here.** Anyone with system access can run the check below on their
download in seconds; the result tells what, if anything, still deviates.

## What was obtained and used instead

| Source | What it establishes | How it was used |
|---|---|---|
| SAP's sample code [`SAP-samples/s4hana-mc-xml-file-splitter`](https://github.com/SAP-samples/s4hana-mc-xml-file-splitter) (Apache-2.0), `src/splitter.py`: SAP's own tool that splits *real* migration cockpit XML files | Worksheet 1 and 2 are copied unchanged, **worksheet 3 is the main sheet, worksheets 4+ are sub sheets**; every data sheet has **exactly 8 header rows and instances from row 9**; the **first cell of row 7 carries `ss:MergeAcross`, and its span + 1 is the number of key columns** (a cell without the attribute means one key); the first key columns of every sheet hold the instance key; the file is **line oriented** (`<Worksheet ss:Name`, `<Row`, `<Cell` with its `<Data>` on one line, `</Table>` scanned per line) | Vendored unchanged under `backend/tests/vendor/sap_xml_splitter/` (with license and notice) and **executed in the test suite on files the export fills**: it must scan the instances, split them and distribute the sub-sheet rows by key |
| SAP's blog "SAP S/4HANA Migration Cockpit – Working with the Excel Template" (2017, on-premise), as summarised by web search; the page itself is unreachable from here | `Field List` sheet with Sheet Name, Group Name, Field Name, Importance, Type, Length, Decimal Places and the **hidden columns 8 `SAP Structure` and 9 `SAP Field`** (technical names, "often the ERP table and field names"); data sheets: **row 4 structure technical name, row 5 technical field names, row 6 type/length (rows 4–6 hidden), row 8 field descriptions with `*` marking mandatory fields**; mandatory sheets orange, optional blue; sheets must not be deleted, renamed or reordered; no formulas, paste values only | Encoded in the parser's recognition rules and in the illustrative samples; every deviation is reported by `check_template` instead of being assumed |
| SAP KBAs 2692715 / 2650960 (titles and abstracts only) | Modified templates are unsupported; a download may lose its `.xml` extension; pasted formatting breaks date cells | README guidance in the export package; the filler writes untyped-free cells (DateTime / Number / String) and touches nothing but the rows below the header |

## What changed after the validation

The first version of the template support assumed technical names in row 8. SAP's documentation puts them in the
hidden row 5, the descriptions in row 8 and the key span in row 7, and SAP's splitter requires eight header rows
and a line-oriented file. Running the splitter on the first version's output exposed three defects, all fixed:

1. **Header rows.** The data start is now the maximum of the technical-name row, the key row, the description row
   and the last hidden header row, so a template with technical names in row 5 keeps rows 6–8 and writes from row
   9 (the first version would have overwritten rows 6–8).
2. **Key columns.** The merged key cell is read like the splitter reads it: `MergeAcross` span + 1, and a span of
   `0` (one key) counts; the first version ignored a one-column key.
3. **Line-oriented output.** `<Row>`, each `<Cell>` with its `<Data>`, `</Row>`, `<Table>`, `<Worksheet>` on their
   own lines, so the splitter (and any line-scanning tool) can process the file; everything else (styles, hidden
   rows, merged cells, column definitions) is preserved.

Two mapping gaps surfaced at the same time: the `SAP Structure` column now selects the staging table directly when
it names a catalog table (`table_by = structure`), and fields that live on a sibling segment of the same instance
(the chart of accounts on the company code sheet) are mapped as `related`.

## What is verified (tests/test_cockpit_templates.py, 8 tests)

* Parsing of the documented layout: hidden rows 4–6, technical names from row 5, descriptions and `*` from row 8,
  key span from row 7 (two keys and one key), `SAP Structure`/`SAP Field` from the hidden Field List columns,
  Importance values, types and lengths.
* `check_template` returns `documented_layout = true` with no warnings for that layout, and names every assumption
  for a deviating file (no Field List, different worksheet order, fewer header rows, no key cell).
* Filling: data from row 9, typed cells, keys never empty (counted when they are), sample rows replaced, hidden
  rows and merged cells and styles preserved, line-oriented output.
* **SAP's splitter processes the filled file unchanged**: five instances scanned, split into two files, each a
  parseable template with eight header rows and one key column, every item row in the same file as its header,
  no `_invalid_data` output.
* Export integration, API (`/cockpit-templates/check`, register with the check verdict, mapping, download) and
  CLI (`sdtf cockpit-template check --file <download> --object <BO>`).

## What is not verified, and how to close it

* **A template downloaded from a release.** The wording of the Field List header (e.g. "Importance" values), the
  exact text of rows 1–3, extra legend rows on the Field List sheet, and the data types SAP prints in row 6 have
  not been seen. The parser recognises columns by several spellings and reports what it did not find; it does not
  guess silently. Run on a download:

  ```bash
  sdtf cockpit-template check --file S4_GL_ACCOUNT.xml --object FI.GLAccount
  ```

  A `documented layout = True` verdict with no warnings means the file matches everything above; warnings name
  the deviation and the assumption made. Please report deviations with the sheet and row concerned.
* **Acceptance by the app.** Whether the app's upload simulation accepts the filled file can only be seen in a
  system. The splitter check covers the structural contract SAP's own tooling applies, not the app's field-level
  validation (value ranges, configuration existence), which the migration cockpit itself performs.
* **Migration object IDs and field semantics.** The `migration_object` text comes from the template's Introduction
  sheet; the BAPI-style alias catalogue is a heuristic that the mapping report labels as `alias` so that each one
  can be checked against the Field List description.

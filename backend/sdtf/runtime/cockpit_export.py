"""Migration cockpit staging-file export (ADR-0015, third step).

A real S/4HANA target fills migration cockpit staging tables through a database connection or through the files
the *Migrate Your Data* app accepts; nothing posts them over HTTPS. This module writes, for one run, the rows the
initial load routes to the cockpit (the same decision the LOAD stage takes through `plan_cockpit`: cockpit-only
objects, tables the document APIs do not expose, histories) as a package of files:

* `<OBJECT>/<TABLE>.csv`         one UTF-8 CSV per staging table, header row, catalog field order;
* `<OBJECT>.xml`                 one SpreadsheetML 2003 workbook per migration object (an Introduction sheet, a
                                 Field List sheet and one sheet per table), the layout the app's XML templates use;
* `manifest.json`                object -> migration object hint, tables, row counts, load statuses, sha256 per file;
* `README.md`                    upload instructions and the honesty notes below;
* `cockpit_<run>.zip`            the package, for download through the API.

What is NOT claimed: the files are not generated from a target's own migration object templates (those are
downloaded from the app per release and differ in sheet and column names). The export carries the transformed
staging-table images with SAP field names; mapping them onto the template of the release in use is a step the
README spells out. Migration object names are hints to verify, not identifiers read from a system.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from sqlalchemy.orm import Session

from .. import config
from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES
from ..models import MigrationRun, SapSystem
from ..staging import get_backend
from .loaders import EventView, object_of, plan_cockpit

# Migration object hints (SAP S/4HANA migration cockpit, "Migrate Your Data" app). Names differ between releases
# and between the cloud and on-premise editions: verify the ID in the target before mapping the files.
MIGRATION_OBJECT_HINTS: dict[str, str] = {
    "FI.GLAccount": "G/L account (company code segment)",
    "FI.AccountingDocument": "FI - Accounts receivable open item / Accounts payable open item / G/L account balance and open item",
    "FI.FixedAsset": "Fixed asset (incl. balances)",
    "MM.MaterialDocument": "Material - Inventory balance (stock on hand); historical movements are not migrated",
    "MM.InvoiceReceipt": "Supplier invoice (open items) / Accounts payable open item",
    "SD.BillingDocument": "no standard migration object for historical billing: custom migration object or archive-like history table",
    "SD.SalesOrder": "Sales order (open) - exported here only as history that the API cannot re-create",
    "MM.PurchaseOrder": "Purchase order (open) / PO history - exported here only as history that the API cannot re-create",
    "SD.Delivery": "no standard migration object for goods-issued deliveries: history",
    "PP.ProductionOrder": "Production order - components and confirmations the API does not expose",
    "Z.ExportControl": "custom migration object (custom table)",
    "Z.TsaScope": "custom migration object (custom table)",
    "Z.SupplierExt": "custom migration object (custom table)",
}
EXPORTED_STATUSES = ("TRANSFORMED", "LOADED", "UNSUPPORTED", "CONFLICT", "REJECTED")  # has a target image; STAGED/SKIPPED/MATCHED have none or are configuration
SS = "urn:schemas-microsoft-com:office:spreadsheet"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _columns(table: str, rows: list[dict]) -> list[str]:
    td = TABLES.get(table)
    cols = list(td.fields) if td else []
    extra = sorted({k for r in rows for k in r} - set(cols))
    return cols + extra


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "X" if v else ""
    if isinstance(v, float):
        return f"{v:.2f}" if v != int(v) else str(int(v))
    return str(v)


def _csv_bytes(cols: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return buf.getvalue().encode("utf-8")


def _sheet(name: str, header: list[str], rows: list[list[str]]) -> str:
    out = [f'<Worksheet ss:Name="{escape(name[:31], {chr(34): "&quot;"})}"><Table>']
    for values in [header, *rows]:
        out.append("<Row>" + "".join(f'<Cell><Data ss:Type="String">{escape(v)}</Data></Cell>' for v in values) + "</Row>")
    out.append("</Table></Worksheet>")
    return "\n".join(out)


def _workbook_bytes(object_type: str, hint: str, run: MigrationRun, tables: dict[str, tuple[list[str], list[dict]]]) -> bytes:
    """SpreadsheetML 2003 workbook (the format the Migrate Your Data app's XML templates use): default namespace
    on the elements, `ss:` prefix on the attributes, string cells so SAP keys keep their leading zeros."""
    bo = BUSINESS_OBJECTS.get(object_type)
    intro = [["Business object", object_type], ["Description", bo.name if bo else ""], ["Migration object (hint, verify in the target release)", hint], ["Run", run.id], ["Snapshot", run.snapshot_id or ""], ["Generated", datetime.now(timezone.utc).isoformat()], ["Sheets", ", ".join(f"{t} ({len(r)} rows)" for t, (_, r) in tables.items())]]
    sheets = [_sheet("Introduction", ["Property", "Value"], intro)]
    fl = []
    for t, (cols, _) in tables.items():
        td = TABLES.get(t)
        for c in cols:
            fl.append([t, c, "X" if td and c in td.key_fields else "", td.description if td else ""])
    sheets.append(_sheet("Field List", ["Table", "Field", "Key", "Table description"], fl))
    for t, (cols, rows) in tables.items():
        sheets.append(_sheet(t, cols, [[_cell(r.get(c)) for c in cols] for r in rows]))
    doc = '<?xml version="1.0" encoding="UTF-8"?>\n<?mso-application progid="Excel.Sheet"?>\n' + f'<Workbook xmlns="{SS}" xmlns:ss="{SS}">\n' + "\n".join(sheets) + "\n</Workbook>\n"
    return doc.encode("utf-8")


def _readme(run: MigrationRun, src: SapSystem | None, tgt: SapSystem | None, objects: dict) -> str:
    lines = [f"# Migration cockpit staging files for run {run.id}", "", f"Source: {src.sid if src else '?'} ({src.logical_system if src else ''}) -> Target: {tgt.sid if tgt else '?'} ({tgt.product if tgt else ''}), snapshot {run.snapshot_id or '-'}.", "",
             "These files hold the transformed staging-table images the initial load routes to the SAP S/4HANA migration cockpit: objects the compatibility registry assigns to the cockpit, tables the released document APIs do not expose, and histories (completed sales orders, fully delivered purchase orders, goods-issued deliveries) whose statuses the APIs cannot set.", "",
             "## How to use them", "1. In the target, open the *Migrate Your Data* app, create a migration project and select the migration objects listed below.", "2. Download the object's XML/CSV template for your release; sheet and column names come from the template, not from here.", "3. Map the columns of each `<TABLE>.csv` (SAP field names, catalog order, keys first) onto the template, or load the CSV into the template with the field list in `<OBJECT>.xml`.", "4. Upload, simulate, then migrate. Record the cockpit's own result in the cutover runbook; this package's `manifest.json` carries a sha256 per file for the evidence package.", "",
             "## Objects", "| Business object | Migration object (hint) | Tables | Rows |", "|---|---|---|---|"]
    for ot, o in objects.items():
        lines.append(f"| {ot} | {o['migration_object']} | {', '.join(o['tables'])} | {o['rows']} |")
    lines += ["", "## What this export is not", "* Not generated from the target's migration object templates: those are release specific and must be downloaded from the app.", "* Migration object names are hints to verify; the platform never read them from a system.", "* Rows whose `load_status` is LOADED were accepted by the simulated gateway's cockpit; on a real target the cockpit's simulation decides.", "* Values are written as the transformed images hold them (dates YYYYMMDD, amounts as decimals, flags X/blank)."]
    return "\n".join(lines) + "\n"


def export_cockpit_files(session: Session, run_id: str, out_dir: str | None = None, actor: str = "system", formats: tuple[str, ...] = ("csv", "xml")) -> dict:
    """Write the package for `run_id` and return its summary (also stored under `run.report['cockpit_export']`)."""
    run = session.get(MigrationRun, run_id)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    src = session.get(SapSystem, run.source_system_id)
    tgt = session.get(SapSystem, run.target_system_id)
    product = tgt.product if tgt else "S4HANA"
    backend = get_backend(session=session)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    order: list[tuple[str, str]] = []
    for rec in backend.iter_records(run_id):
        if rec.load_status not in EXPORTED_STATUSES or not rec.target_payload:
            continue
        g = object_of(rec.table_name, rec.target_key or rec.record_key, rec.target_payload)
        if g not in groups:
            order.append(g)
        groups[g].append(rec)
    # route exactly as the LOAD stage does
    per_object: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    statuses: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    labels: dict[str, set[str]] = defaultdict(set)
    for g in order:
        members = groups[g]
        bo = BUSINESS_OBJECTS.get(g[0])
        rank = {bo.header_table: 0, **{t: i + 1 for i, t in enumerate(bo.item_tables)}} if bo else {}
        members.sort(key=lambda r: (rank.get(r.table_name, 99), r.target_key or r.record_key))
        views = [EventView(0, r.table_name, "I", r.record_key, r.target_key, dict(r.target_payload), g[0], g[1], g[1]) for r in members]
        cockpit, _rest = plan_cockpit(g[0], views, product)
        by_key = {(r.table_name, r.record_key): r for r in members}
        for evs, label in cockpit:
            labels[g[0]].add(label)
            for e in evs:
                per_object[g[0]][e.table].append(e.target_payload)
                statuses[g[0]][by_key[(e.table, e.record_key)].load_status] += 1
    base = os.path.join(out_dir or os.path.join(config.settings.evidence_dir, "cockpit"), run_id)
    if os.path.isdir(base):
        shutil.rmtree(base)
    os.makedirs(base, exist_ok=True)
    files: dict[str, dict] = {}
    objects: dict[str, dict] = {}

    def put(rel: str, data: bytes) -> None:
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        files[rel] = {"sha256": _sha(data), "bytes": len(data)}

    for ot in sorted(per_object):
        bo = BUSINESS_OBJECTS.get(ot)
        rank = {bo.header_table: 0, **{t: i + 1 for i, t in enumerate(bo.item_tables)}} if bo else {}
        tables = {t: (_columns(t, per_object[ot][t]), per_object[ot][t]) for t in sorted(per_object[ot], key=lambda t: (rank.get(t, 99), t))}
        for t, (cols, rows) in tables.items():
            td = TABLES.get(t)
            keys = td.key_fields if td else ()
            rows.sort(key=lambda r: tuple(_cell(r.get(k)) for k in keys))
            if "csv" in formats:
                put(f"{ot}/{t}.csv", _csv_bytes(cols, rows))
        hint = MIGRATION_OBJECT_HINTS.get(ot, "verify the migration object for this table in the target release")
        if "xml" in formats:
            put(f"{ot}.xml", _workbook_bytes(ot, hint, run, tables))
        objects[ot] = {"migration_object": hint, "reasons": sorted(labels[ot]), "tables": list(tables), "rows": sum(len(r) for _, r in tables.values()), "rows_by_table": {t: len(r) for t, (_, r) in tables.items()}, "load_status": dict(statuses[ot])}
    readme = _readme(run, src, tgt, objects)
    put("README.md", readme.encode("utf-8"))
    manifest = {"run_id": run.id, "project_id": run.project_id, "snapshot_id": run.snapshot_id, "source": {"sid": src.sid, "logical_system": src.logical_system} if src else None, "target": {"sid": tgt.sid, "product": tgt.product} if tgt else None, "generated_at": datetime.now(timezone.utc).isoformat(), "formats": list(formats), "objects": objects, "files": dict(sorted(files.items())), "rows": sum(o["rows"] for o in objects.values()), "disclaimer": "Transformed staging images routed to the migration cockpit; not generated from the target's migration object templates. Verify migration object IDs in the target release."}
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True, default=str).encode("utf-8")
    with open(os.path.join(base, "manifest.json"), "wb") as fh:
        fh.write(manifest_bytes)
    zip_path = os.path.join(base, f"cockpit_{run.id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            zf.write(os.path.join(base, rel), rel)
        zf.write(os.path.join(base, "manifest.json"), "manifest.json")
    summary = {"exported": True, "dir": base, "zip": zip_path, "manifest_sha256": _sha(manifest_bytes), "generated_at": manifest["generated_at"], "objects": {ot: {k: v for k, v in o.items() if k != "rows_by_table"} for ot, o in objects.items()}, "files": len(files) + 1, "rows": manifest["rows"], "formats": list(formats)}
    run.report = {**(run.report or {}), "cockpit_export": summary}
    record_event(session, actor, "COCKPIT_EXPORTED", "RUN", run.id, {"objects": len(objects), "rows": summary["rows"], "files": summary["files"], "manifest_sha256": summary["manifest_sha256"]})
    session.flush()
    return summary


def cockpit_export_summary(run: MigrationRun) -> dict | None:
    return (run.report or {}).get("cockpit_export")

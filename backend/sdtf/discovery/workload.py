"""Workload statistics from an ST03N transaction-profile export, compared with the database footprint.

Workload statistics are not table reads: the read-only add-on cannot see them. A Basis administrator exports the
ST03N transaction profile (a delimited text or spreadsheet export with a header) and imports it here; the
platform never guesses what was executed. The import maps the transactions it knows to the platform's business
objects and process tables, so the executed dialog steps can be put next to the documents in the database.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..models import SapSystem

# transaction code -> (business object, process table, area, meaning)
TRANSACTIONS: dict[str, tuple[str, str, str, str]] = {
    "VA01": ("SD.SalesOrder", "VBAK", "O2C", "create sales order"), "VA02": ("SD.SalesOrder", "VBAK", "O2C", "change sales order"), "VA03": ("SD.SalesOrder", "VBAK", "O2C", "display sales order"), "VA05": ("SD.SalesOrder", "VBAK", "O2C", "list sales orders"),
    "VL01N": ("SD.Delivery", "LIKP", "O2C", "create outbound delivery"), "VL02N": ("SD.Delivery", "LIKP", "O2C", "change outbound delivery"), "VL03N": ("SD.Delivery", "LIKP", "O2C", "display outbound delivery"), "VL10": ("SD.Delivery", "LIKP", "O2C", "delivery due list"),
    "VF01": ("SD.BillingDocument", "VBRK", "O2C", "create billing document"), "VF02": ("SD.BillingDocument", "VBRK", "O2C", "change billing document"), "VF03": ("SD.BillingDocument", "VBRK", "O2C", "display billing document"), "VF04": ("SD.BillingDocument", "VBRK", "O2C", "billing due list"),
    "ME21N": ("MM.PurchaseOrder", "EKKO", "P2P", "create purchase order"), "ME22N": ("MM.PurchaseOrder", "EKKO", "P2P", "change purchase order"), "ME23N": ("MM.PurchaseOrder", "EKKO", "P2P", "display purchase order"), "ME29N": ("MM.PurchaseOrder", "EKKO", "P2P", "release purchase order"), "ME2L": ("MM.PurchaseOrder", "EKKO", "P2P", "purchase orders by vendor"),
    "MIGO": ("MM.MaterialDocument", "MKPF", "P2P", "goods movement"), "MB1A": ("MM.MaterialDocument", "MKPF", "P2P", "goods issue"), "MB1B": ("MM.MaterialDocument", "MKPF", "P2P", "transfer posting"), "MB1C": ("MM.MaterialDocument", "MKPF", "P2P", "goods receipt"), "MB51": ("MM.MaterialDocument", "MKPF", "P2P", "material document list"),
    "MIRO": ("MM.Invoice", "RBKP", "P2P", "enter incoming invoice"), "MIR4": ("MM.Invoice", "RBKP", "P2P", "display invoice document"), "MIR7": ("MM.Invoice", "RBKP", "P2P", "park invoice"),
    "FB01": ("FI.AccountingDocument", "BKPF", "R2R", "post document"), "FB50": ("FI.AccountingDocument", "BKPF", "R2R", "G/L account posting"), "FB60": ("FI.AccountingDocument", "BKPF", "R2R", "vendor invoice"), "FB70": ("FI.AccountingDocument", "BKPF", "R2R", "customer invoice"), "FB03": ("FI.AccountingDocument", "BKPF", "R2R", "display document"), "FB02": ("FI.AccountingDocument", "BKPF", "R2R", "change document"), "F-02": ("FI.AccountingDocument", "BKPF", "R2R", "G/L posting"), "F-28": ("FI.AccountingDocument", "BKPF", "R2R", "incoming payment"), "F-53": ("FI.AccountingDocument", "BKPF", "R2R", "outgoing payment"), "F110": ("FI.AccountingDocument", "BKPF", "R2R", "payment run"), "FBL1N": ("FI.AccountingDocument", "BKPF", "R2R", "vendor line items"), "FBL3N": ("FI.AccountingDocument", "BKPF", "R2R", "G/L line items"), "FBL5N": ("FI.AccountingDocument", "BKPF", "R2R", "customer line items"), "FAGLL03": ("FI.AccountingDocument", "BKPF", "R2R", "G/L line items (new GL)"),
    "XD01": ("MD.Customer", "KNA1", "O2C", "create customer"), "XD02": ("MD.Customer", "KNA1", "O2C", "change customer"), "XD03": ("MD.Customer", "KNA1", "O2C", "display customer"), "FD01": ("MD.Customer", "KNA1", "R2R", "create customer (accounting)"), "FD02": ("MD.Customer", "KNA1", "R2R", "change customer (accounting)"), "BP": ("MD.Customer", "KNA1", "O2C", "business partner"),
    "XK01": ("MD.Vendor", "LFA1", "P2P", "create vendor"), "XK02": ("MD.Vendor", "LFA1", "P2P", "change vendor"), "XK03": ("MD.Vendor", "LFA1", "P2P", "display vendor"), "FK01": ("MD.Vendor", "LFA1", "R2R", "create vendor (accounting)"), "FK02": ("MD.Vendor", "LFA1", "R2R", "change vendor (accounting)"),
    "MM01": ("MD.Material", "MARA", "P2P", "create material"), "MM02": ("MD.Material", "MARA", "P2P", "change material"), "MM03": ("MD.Material", "MARA", "P2P", "display material"), "MM60": ("MD.Material", "MARA", "P2P", "materials list"),
    "CO01": ("PP.ProductionOrder", "AFKO", "P2P", "create production order"), "CO02": ("PP.ProductionOrder", "AFKO", "P2P", "change production order"), "CO03": ("PP.ProductionOrder", "AFKO", "P2P", "display production order"), "CO11N": ("PP.ProductionOrder", "AFKO", "P2P", "confirm production order"), "CO15": ("PP.ProductionOrder", "AFKO", "P2P", "confirm order"),
    "AS01": ("FI.FixedAsset", "ANLA", "R2R", "create asset"), "AS02": ("FI.FixedAsset", "ANLA", "R2R", "change asset"), "AS03": ("FI.FixedAsset", "ANLA", "R2R", "display asset"), "AW01N": ("FI.FixedAsset", "ANLA", "R2R", "asset explorer"), "AFAB": ("FI.FixedAsset", "ANLA", "R2R", "depreciation run"),
    "KS01": ("CO.CostCenter", "CSKS", "R2R", "create cost center"), "KS02": ("CO.CostCenter", "CSKS", "R2R", "change cost center"), "KS03": ("CO.CostCenter", "CSKS", "R2R", "display cost center"), "KSB1": ("CO.CostCenter", "CSKS", "R2R", "cost center line items"),
}
# a transaction's kind from its meaning: dialog that changes data, or reads / lists
WRITE_WORDS = ("create", "change", "post", "release", "confirm", "enter", "park", "run", "payment", "movement", "issue", "receipt", "transfer")

TCODE_HEADERS = ("transaction", "tcode", "transaction code", "transaction/report", "report", "tcode/report", "task type")
STEPS_HEADERS = ("# steps", "steps", "dialog steps", "no. of steps", "number of steps", "#steps", "dialogsteps", "dialog steps (no.)", "total steps")
MEASURE_HEADERS = {"response time": "response_ms_total", "t response time": "response_ms_total", "ø time": "response_ms_avg", "avg. response time": "response_ms_avg", "ø resp. time": "response_ms_avg", "cpu time": "cpu_ms_total", "ø cpu time": "cpu_ms_avg", "db time": "db_ms_total", "ø db time": "db_ms_avg", "users": "users", "# users": "users"}


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(" ", "")
    if not s:
        return None
    # SAP exports: 1.234.567,89 or 1,234,567.89 or 1234567
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d+", s):
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_st03n(text: str) -> dict:
    """Rows of an ST03N transaction-profile export: a header row with the transaction column and the dialog-step
    column is required; other numeric columns are kept as measures when their header is known."""
    text = text.lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip() and not set(ln.strip()) <= set("-|=")]
    if not lines:
        return {"rows": [], "errors": ["the export holds no rows"], "columns": [], "delimiter": None}
    sample = "\n".join(lines[:20])
    delimiter = max(("\t", ";", ",", "|"), key=lambda d: sample.count(d))
    if sample.count(delimiter) == 0:
        return {"rows": [], "errors": ["no delimiter found (tab, semicolon, comma or pipe)"], "columns": [], "delimiter": None}
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    # header: the first row that names the transaction column
    hdr_i = next((i for i, r in enumerate(rows[:10]) if any(c.strip().lower() in TCODE_HEADERS for c in r)), None)
    if hdr_i is None:
        return {"rows": [], "errors": [f"no header row naming the transaction column (one of {', '.join(TCODE_HEADERS)})"], "columns": [], "delimiter": delimiter}
    header = [c.strip() for c in rows[hdr_i]]
    lower = [c.lower() for c in header]
    ti = next(i for i, c in enumerate(lower) if c in TCODE_HEADERS)
    si = next((i for i, c in enumerate(lower) if c in STEPS_HEADERS), None)
    if si is None:
        return {"rows": [], "errors": [f"no dialog-step column (one of {', '.join(STEPS_HEADERS)})"], "columns": header, "delimiter": delimiter}
    measures = {i: MEASURE_HEADERS[c] for i, c in enumerate(lower) if c in MEASURE_HEADERS}
    out, errors, skipped = [], [], 0
    for n, r in enumerate(rows[hdr_i + 1 :], start=hdr_i + 2):
        if len(r) <= max(ti, si):
            skipped += 1
            continue
        tcode = r[ti].strip().upper()
        steps = _num(r[si])
        if not tcode or steps is None:
            skipped += 1
            continue
        if tcode in ("TOTAL", "SUM", "*", "GESAMT"):
            continue
        row = {"tcode": tcode, "steps": int(steps)}
        for i, name in measures.items():
            if i < len(r):
                v = _num(r[i])
                if v is not None:
                    row[name] = v
        out.append(row)
    if skipped:
        errors.append(f"{skipped} row(s) skipped: no transaction or no numeric step count")
    return {"rows": out, "errors": errors, "columns": header, "delimiter": delimiter, "header_row": hdr_i + 1}


def compare(rows: list[dict], variants: list[dict] | None = None, growth: list[dict] | None = None) -> dict:
    """Executed dialog steps next to the documents in the database, per business object; unmapped transactions
    listed by steps so the catalogue can be extended."""
    total_steps = sum(r["steps"] for r in rows)
    by_obj: dict[str, dict] = {}
    unmapped: dict[str, int] = {}
    for r in rows:
        t = TRANSACTIONS.get(r["tcode"])
        if t is None:
            unmapped[r["tcode"]] = unmapped.get(r["tcode"], 0) + r["steps"]
            continue
        obj, table, area, meaning = t
        write = any(w in meaning for w in WRITE_WORDS) and "display" not in meaning and "list" not in meaning and "items" not in meaning
        d = by_obj.setdefault(obj, {"object": obj, "table": table, "area": area, "steps": 0, "write_steps": 0, "read_steps": 0, "transactions": {}})
        d["steps"] += r["steps"]
        d["write_steps" if write else "read_steps"] += r["steps"]
        d["transactions"][r["tcode"]] = d["transactions"].get(r["tcode"], 0) + r["steps"]
    docs = {}
    for v in variants or []:
        docs[v["table"]] = max(docs.get(v["table"], 0), int(v.get("total", 0)))
    for g in growth or []:
        docs.setdefault(g["table"], int(g.get("rows", 0)))
    out = []
    for d in by_obj.values():
        n = docs.get(d["table"])
        d["documents_in_db"] = n
        d["write_steps_per_document"] = round(d["write_steps"] / n, 2) if n else None
        d["share_of_steps"] = round(d["steps"] / total_steps, 4) if total_steps else 0.0
        d["transactions"] = dict(sorted(d["transactions"].items(), key=lambda kv: -kv[1]))
        out.append(d)
    out.sort(key=lambda d: -d["steps"])
    mapped = sum(d["steps"] for d in out)
    return {"total_steps": total_steps, "mapped_steps": mapped, "mapped_share": round(mapped / total_steps, 4) if total_steps else 0.0, "transactions": len(rows), "transactions_mapped": sum(1 for r in rows if r["tcode"] in TRANSACTIONS), "by_object": out, "unmapped_top": [{"tcode": k, "steps": v} for k, v in sorted(unmapped.items(), key=lambda kv: -kv[1])[:25]]}


def import_workload(session: Session, system: SapSystem, text: str, period: str, actor: str, source_file: str = "") -> dict:
    """Parse and keep the export on the system (meta.workload): the rows, the period it covers, who imported it."""
    parsed = parse_st03n(text)
    if not parsed["rows"]:
        raise ValueError("; ".join(parsed["errors"]) or "no usable rows")
    meta = dict(system.meta or {})
    meta["workload"] = {"period": (period or "").strip()[:60], "imported_at": datetime.now(UTC).isoformat(), "by": actor, "source_file": source_file[:120], "columns": parsed["columns"], "delimiter": parsed["delimiter"], "rows": parsed["rows"][:5000], "row_count": len(parsed["rows"]), "total_steps": sum(r["steps"] for r in parsed["rows"]), "warnings": parsed["errors"]}
    system.meta = meta
    session.flush()
    record_event(session, actor, "WORKLOAD_IMPORTED", "SYSTEM", system.id, {"period": meta["workload"]["period"], "rows": len(parsed["rows"]), "total_steps": meta["workload"]["total_steps"], "source_file": source_file[:120]})
    return meta["workload"]


def table_rows(session: Session, system: SapSystem) -> list[dict]:
    """{table, rows} from the latest discovery statistics: the documents in the database for the comparison."""
    from sqlalchemy import select

    from ..models import TableStatistic
    from .service import latest_snapshot

    snap = latest_snapshot(session, system.id)
    if snap is None:
        return []
    return [{"table": ts.table_name, "rows": ts.row_count} for ts in session.execute(select(TableStatistic).where(TableStatistic.snapshot_id == snap.id)).scalars().all()]


def usage_section(system: SapSystem, variants: list[dict] | None = None, growth: list[dict] | None = None) -> dict:
    """The usage block of the process analysis: the imported ST03N profile compared with the footprint, or the
    honest statement that nothing was imported."""
    w = (system.meta or {}).get("workload")
    if not w:
        return None
    cmp = compare(w.get("rows", []), variants, growth)
    return {"status": "IMPORTED", "source": "ST03N transaction profile export", "period": w.get("period", ""), "imported_at": w.get("imported_at"), "by": w.get("by"), "source_file": w.get("source_file", ""), "transactions": ["ST03N"], "reason": "Executed dialog steps from the imported ST03N transaction profile, mapped to the platform's business objects by transaction code and put next to the documents in the database. Workflow logs (SWI1, SWI2_FREQ) and IDoc monitors (WE02, BD87) are still not read: the discovery lists the RFC destinations, IDoc partners and background jobs.", **cmp}


__all__ = ["TRANSACTIONS", "parse_st03n", "compare", "import_workload", "usage_section", "table_rows"]

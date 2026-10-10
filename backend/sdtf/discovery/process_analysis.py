"""Business process analysis from the database footprint: what the classic Basis transactions show (TAANA
distributions, DB05 selectivity, DB02 growth, DB15 table-to-object links, the age profile of the documents),
computed through the read-only add-on's aggregate module (`Z_SDTF_AGGREGATE`: COUNT per group, pushed down to
the system) so the same analysis runs on the simulated landscape and on a live NPL / A4H. Workload statistics
(ST03N, STAD), workflow logs (SWI1, SWI2_FREQ) and IDoc monitors (WE02, BD87) are not table reads and are
reported as not available rather than guessed."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES
from ..discovery.service import latest_snapshot
from ..models import SapSystem, TableStatistic
from ..runtime.addon import client_for
from ..runtime.rfc import RfcError, RfcUnavailable, predicate
from .workload import table_rows, usage_section

AREAS = ("ALL", "O2C", "P2P", "R2R")

# TAANA-style distributions: (area, table, group fields, what the variants mean)
VARIANTS: list[tuple[str, str, list[str], str]] = [
    ("O2C", "VBAK", ["AUART"], "sales order types: which order process variants carry the volume"),
    ("O2C", "VBAK", ["VKORG", "AUART"], "order types per sales organisation: where a variant is used"),
    ("O2C", "LIKP", ["LFART"], "delivery types"),
    ("O2C", "VBRK", ["FKART"], "billing types"),
    ("P2P", "EKKO", ["BSART"], "purchasing document types"),
    ("P2P", "EKKO", ["EKORG", "BSART"], "purchasing document types per purchasing organisation"),
    ("P2P", "MSEG", ["BWART"], "goods movement types: receipts, issues, transfers, reversals"),
    ("P2P", "RBKP", ["RBSTAT"], "invoice receipt statuses"),
    ("R2R", "BKPF", ["BLART"], "accounting document types: which postings make the journal"),
    ("R2R", "BKPF", ["BUKRS", "BLART"], "document types per company code"),
    ("R2R", "BKPF", ["AWTYP"], "originating applications of the postings (reference transaction)"),
    ("R2R", "BSEG", ["KOART"], "account types of the line items (customer, vendor, G/L, asset, material)"),
]

# DB05-style selectivity: (area, table, field, meaning)
SELECTIVITY: list[tuple[str, str, str, str]] = [
    ("O2C", "VBAK", "KUNNR", "sold-to customers behind the orders"),
    ("O2C", "VBAP", "MATNR", "materials sold"),
    ("P2P", "EKKO", "LIFNR", "vendors behind the purchase orders"),
    ("P2P", "MSEG", "MATNR", "materials moved"),
    ("R2R", "BSEG", "HKONT", "G/L accounts posted to"),
    ("R2R", "BSEG", "KUNNR", "customers with line items"),
    ("R2R", "BSEG", "LIFNR", "vendors with line items"),
]

# age profiles: (area, table, year field)
AGE: list[tuple[str, str, str]] = [("R2R", "BKPF", "GJAHR"), ("O2C", "VBRK", "GJAHR"), ("P2P", "EKKO", "GJAHR"), ("P2P", "MSEG", "MJAHR")]

# the company-code field per table for the pushdown
CC_FIELD = {"VBAK": "BUKRS_VF", "VBAP": None, "LIKP": "BUKRS", "VBRK": "BUKRS", "EKKO": "BUKRS", "MSEG": "BUKRS", "RBKP": "BUKRS", "BKPF": "BUKRS", "BSEG": "BUKRS"}

USAGE_NOT_AVAILABLE = {
    "status": "NOT_AVAILABLE",
    "reason": "Workload statistics (ST03N transaction profile, STAD business transaction analysis), workflow logs (SWI1, SWI2_FREQ) and IDoc monitors (WE02, BD87) are not table reads: they live in the statistics cluster, the workflow runtime and the IDoc status tables the read-only add-on does not expose. Export the ST03N transaction profile from the system and compare it with the variants here; an import of that export is planned, never a guess.",
    "transactions": ["ST03N", "STAD", "SWI1", "SWI2_FREQ", "WE02", "BD87"],
}

MATRIX = [
    {"objective": "Identify process variances", "transactions": "TAANA", "here": "variants: the distribution of a process table by its type fields (order type, document type, movement type), with the variants that make 80 % of the volume"},
    {"objective": "Understand data selectivity", "transactions": "DB05", "here": "selectivity: distinct customers, vendors, materials and accounts behind the transactional tables, and the share of the top value"},
    {"objective": "Detect process overloads", "transactions": "DB02 + DB15", "here": "growth: rows per fiscal year from the discovery statistics with the year-over-year change; links: which business object populates each table"},
    {"objective": "Size the archiving and the historical scope", "transactions": "TAANA + DB15", "here": "age: documents per year and the share older than the retention horizon, per process table"},
    {"objective": "Understand user execution", "transactions": "ST03N, STAD", "here": "not readable through the add-on; an ST03N transaction-profile export imported here is compared with the footprint (usage)"},
    {"objective": "Workflow and interface frequencies", "transactions": "SWI1, SWI2_FREQ, WE02, BD87", "here": "not available through the add-on; the discovery lists the RFC destinations, IDoc partners and background jobs"},
]


def _pareto(rows: list[dict], fields: list[str], top: int) -> dict:
    total = sum(int(r["COUNT"]) for r in rows)
    ordered = sorted(rows, key=lambda r: -int(r["COUNT"]))
    out, cum, pareto_n = [], 0, None
    for i, r in enumerate(ordered):
        n = int(r["COUNT"])
        cum += n
        if pareto_n is None and total and cum / total >= 0.8:
            pareto_n = i + 1
        if i < top:
            out.append({"variant": " / ".join(str(r.get(f, "")) for f in fields), **{f: r.get(f, "") for f in fields}, "count": n, "share": round(n / total, 4) if total else 0.0, "cumulative": round(cum / total, 4) if total else 0.0})
    return {"total": total, "variants": len(rows), "pareto_variants": pareto_n if total else 0, "top": out}


def analyse(session: Session, system: SapSystem, area: str = "ALL", company_codes: list[str] | None = None, top: int = 10, retention_years: int = 7, year: int | None = None) -> dict:
    """The analysis document for one system: variants, selectivity, age, growth, links, usage, the matrix."""
    area = (area or "ALL").upper()
    if area not in AREAS:
        raise ValueError(f"area must be one of {', '.join(AREAS)}")
    ccs = [str(c) for c in (company_codes or []) if str(c)]
    sel_v = [v for v in VARIANTS if area == "ALL" or v[0] == area]
    sel_s = [s for s in SELECTIVITY if area == "ALL" or s[0] == area]
    sel_a = [a for a in AGE if area == "ALL" or a[0] == area]
    tables = sorted({v[1] for v in sel_v} | {s[1] for s in sel_s} | {a[1] for a in sel_a})
    out: dict = {"system": {"id": system.id, "sid": system.sid, "client": system.client, "product": system.product, "connector": system.connector}, "area": area, "company_codes": ccs, "top": top, "retention_years": retention_years, "analysed_at": datetime.now(UTC).isoformat(), "variants": [], "selectivity": [], "age": [], "growth": [], "links": [], "usage": USAGE_NOT_AVAILABLE, "matrix": MATRIX, "errors": {}}
    try:
        client, transport = client_for(session, system, tables)
    except (RfcError, RfcUnavailable) as e:
        out["errors"]["connection"] = str(e)
        out["transport"] = None
        out["growth"], out["links"] = growth(session, system), links(tables)
        out["usage"] = usage_section(system, out["variants"], table_rows(session, system)) or USAGE_NOT_AVAILABLE
        return out
    out["transport"] = transport

    def preds(table: str) -> list[dict]:
        f = CC_FIELD.get(table)
        return [predicate(f, "EQ", c) for c in ccs] if f and ccs else []

    for _a, table, fields, meaning in sel_v:
        try:
            rows = client.aggregate(table, preds(table), fields, [])
            out["variants"].append({"table": table, "fields": fields, "meaning": meaning, "description": TABLES[table].description if table in TABLES else "", "pushdown": bool(preds(table)), **_pareto(rows, fields, top)})
        except RfcError as e:
            out["errors"][f"{table}:{'+'.join(fields)}"] = f"{e.key}: {e.message}"
    for _a, table, field, meaning in sel_s:
        try:
            rows = client.aggregate(table, preds(table), [field], [])
            total = sum(int(r["COUNT"]) for r in rows)
            distinct = sum(1 for r in rows if str(r.get(field, "")) != "")
            top_row = max(rows, key=lambda r: int(r["COUNT"])) if rows else None
            out["selectivity"].append({"table": table, "field": field, "meaning": meaning, "rows": total, "distinct": distinct, "selectivity": round(distinct / total, 4) if total else 0.0, "rows_per_value": round(total / distinct, 1) if distinct else 0.0, "top_value": str(top_row.get(field, "")) if top_row else "", "top_share": round(int(top_row["COUNT"]) / total, 4) if top_row and total else 0.0, "pushdown": bool(preds(table))})
        except RfcError as e:
            out["errors"][f"{table}:{field}"] = f"{e.key}: {e.message}"
    horizon = (year or datetime.now(UTC).year) - retention_years
    for _a, table, yfield in sel_a:
        try:
            rows = client.aggregate(table, preds(table), [yfield], [])
            by_year = {str(r.get(yfield, "")): int(r["COUNT"]) for r in rows}
            total = sum(by_year.values())
            old = sum(n for y, n in by_year.items() if y.isdigit() and int(y) < horizon)
            out["age"].append({"table": table, "year_field": yfield, "by_year": dict(sorted(by_year.items())), "rows": total, "older_than_horizon": old, "archivable_share": round(old / total, 4) if total else 0.0, "horizon_year": horizon, "pushdown": bool(preds(table))})
        except RfcError as e:
            out["errors"][f"{table}:{yfield}"] = f"{e.key}: {e.message}"
    out["growth"] = growth(session, system)
    out["links"] = links(tables)
    out["usage"] = usage_section(system, out["variants"], table_rows(session, system)) or USAGE_NOT_AVAILABLE
    out["rfc_calls"], out["snapshot"] = client.calls, client.snapshot
    return out


def growth(session: Session, system: SapSystem) -> list[dict]:
    """DB02-style growth from the discovery statistics: rows per fiscal year of every table that carries a year,
    the last two years compared, ranked by the latest year's volume."""
    snap = latest_snapshot(session, system.id)
    if snap is None:
        return []
    out = []
    for t in session.execute(select(TableStatistic).where(TableStatistic.snapshot_id == snap.id)).scalars().all():
        years = {str(k): int(v) for k, v in (t.by_fiscal_year or {}).items() if str(k).isdigit()}
        if not years:
            continue
        ys = sorted(years)
        last, prev = ys[-1], ys[-2] if len(ys) > 1 else None
        change = (years[last] - years[prev]) / years[prev] if prev and years[prev] else None
        out.append({"table": t.table_name, "description": TABLES[t.table_name].description if t.table_name in TABLES else "", "rows": t.row_count, "est_bytes": t.est_bytes, "latest_year": last, "latest_rows": years[last], "previous_year": prev, "previous_rows": years.get(prev) if prev else None, "change": round(change, 4) if change is not None else None, "by_year": {y: years[y] for y in ys}, "objects": [b.id for b in BUSINESS_OBJECTS.values() if t.table_name == b.header_table or t.table_name in b.item_tables]})
    return sorted(out, key=lambda r: -r["latest_rows"])


def links(tables: list[str] | None = None) -> list[dict]:
    """DB15-style links: which business objects populate which tables (from the canonical model)."""
    out = []
    for b in BUSINESS_OBJECTS.values():
        for table, role in [(b.header_table, "header")] + [(t, "item") for t in b.item_tables]:
            if tables and table not in tables:
                continue
            out.append({"table": table, "role": role, "object": b.id, "object_name": b.name, "domain": b.domain, "kind": b.kind})
    return sorted(out, key=lambda r: (r["table"], r["role"] != "header", r["object"]))


def report_markdown(res: dict) -> str:
    md = [f"# Business process analysis: {res['system']['sid']}/{res['system']['client']} ({res['area']})", "", f"Analysed at {res['analysed_at']} through {res.get('transport') or 'no transport'}; company codes: {', '.join(res['company_codes']) or 'all'}; retention horizon {res['retention_years']} years.", ""]
    md += ["## Process variants (TAANA)", ""]
    for v in res["variants"]:
        md.append(f"### {v['table']} by {' + '.join(v['fields'])}: {v['meaning']}")
        md.append(f"{v['total']} rows, {v['variants']} variants, {v['pareto_variants']} make 80 % of the volume")
        md.append("")
        md.append("| variant | rows | share | cumulative |")
        md.append("|---|---:|---:|---:|")
        for t in v["top"]:
            md.append(f"| {t['variant']} | {t['count']} | {t['share']:.1%} | {t['cumulative']:.1%} |")
        md.append("")
    md += ["## Selectivity (DB05)", "", "| table | field | rows | distinct | rows per value | top value | top share |", "|---|---|---:|---:|---:|---|---:|"]
    md += [f"| {s['table']} | {s['field']} | {s['rows']} | {s['distinct']} | {s['rows_per_value']} | {s['top_value']} | {s['top_share']:.1%} |" for s in res["selectivity"]]
    md += ["", "## Age profile", "", "| table | rows | older than horizon | archivable share | by year |", "|---|---:|---:|---:|---|"]
    for a in res["age"]:
        years = ", ".join(f"{y}: {n}" for y, n in a["by_year"].items())
        md.append(f"| {a['table']} | {a['rows']} | {a['older_than_horizon']} | {a['archivable_share']:.1%} | {years} |")
    md += ["", "## Growth (DB02)", "", "| table | rows | latest year | rows | previous | change | objects |", "|---|---:|---|---:|---:|---:|---|"]
    for g in res["growth"][:25]:
        change = f"{g['change']:+.1%}" if g["change"] is not None else "-"
        prev = g["previous_rows"] if g["previous_rows"] is not None else "-"
        md.append(f"| {g['table']} | {g['rows']} | {g['latest_year']} | {g['latest_rows']} | {prev} | {change} | {', '.join(g['objects'])} |")
    md += ["", "## Usage", "", res["usage"]["reason"], ""]
    if res["usage"].get("status") == "IMPORTED":
        u = res["usage"]
        md += [f"ST03N transaction profile ({u.get('period') or 'period not given'}), imported {str(u.get('imported_at', ''))[:16]} by {u.get('by')}: {u['total_steps']} dialog steps in {u['transactions']} transactions, {u['mapped_share']:.0%} mapped to business objects.", "", "| Business object | Table | Steps | Write steps | Documents in DB | Write steps / document | Transactions |", "|---|---|---|---|---|---|---|"]
        for d in u["by_object"]:
            md.append(f"| {d['object']} | {d['table']} | {d['steps']} | {d['write_steps']} | {d['documents_in_db'] if d['documents_in_db'] is not None else '-'} | {d['write_steps_per_document'] if d['write_steps_per_document'] is not None else '-'} | {', '.join(f'{k} {v}' for k, v in list(d['transactions'].items())[:4])} |")
        if u["unmapped_top"]:
            md += ["", "Not mapped to a business object: " + ", ".join(f"{x['tcode']} ({x['steps']})" for x in u["unmapped_top"][:10]), ""]
    if res["errors"]:
        md += ["## Not readable", ""] + [f"- {k}: {v}" for k, v in res["errors"].items()] + [""]
    return "\n".join(md)

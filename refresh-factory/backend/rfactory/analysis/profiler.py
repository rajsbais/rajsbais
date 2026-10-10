"""Data analysis of a source system: how the rows of a table are distributed, how selective a field combination is, and how a table grows.

These are the questions a business-process review asks of the database before anything is copied or archived (which order types hold most of the
rows, how many distinct customers there are, which months are growing). They are computed from the rows the adapter returns, so they are exactly as
complete as that read: a remote system's scan is capped by its connection profile, and every result says how many rows it looked at.

Nothing here reads runtime statistics (transaction profiles, workflow logs, interface monitors): those are not table data and are not available.
Fields that the masking catalogue classifies as personal data are never grouped by value: the distribution is refused, and a selectivity result
reports counts only.
"""
from __future__ import annotations

from collections import Counter

from ..masking.engine import CATALOG, HR_TABLES
from ..sap.ddic import TABLES

MAX_ROWS = 500_000
MAX_FIELDS = 3
DATE_FIELDS = ("ERDAT", "ERSDA", "BUDAT", "BEDAT", "BLDAT", "QMDAT", "ENSTEHDAT", "GSTRP", "ANSDT", "VDATUM", "PRUEFDATUV", "DATUV", "AEDAT")
COVER = (50, 80, 95)


class AnalysisError(ValueError):
    pass


def is_sensitive(table: str, field: str) -> bool:
    return (table, field) in CATALOG or table in HR_TABLES


def _check(table: str, fields: list[str], *, values: bool, allow_hr: bool) -> None:
    td = TABLES.get(table)
    if td is None or td.config:
        raise AnalysisError(f"{table} is not a table the platform analyses")
    if table in HR_TABLES and not allow_hr:
        raise AnalysisError(f"{table} holds special-category personal data and needs the hr:copy permission")
    if not fields or len(fields) > MAX_FIELDS:
        raise AnalysisError(f"choose one to {MAX_FIELDS} fields")
    if len(set(fields)) != len(fields):
        raise AnalysisError("a field was chosen twice")
    for f in fields:
        if f not in td.fields:
            raise AnalysisError(f"{table} has no field {f}")
        if values and is_sensitive(table, f):
            raise AnalysisError(f"{table}-{f} is classified as personal data: its values are never grouped. Group by a non-personal field")


def _cell(v) -> str:
    s = "" if v is None else str(v).strip()
    return s if s else "(blank)"


def _cut(rows: list[dict]) -> tuple[list[dict], bool]:
    return (rows[:MAX_ROWS], len(rows) > MAX_ROWS)


def distribution(table: str, rows: list[dict], fields: list[str], top: int = 20, allow_hr: bool = False) -> dict:
    """Rows per combination of field values (TAANA style), largest first, with the cumulative share and the number of combinations that cover
    50/80/95 percent of the rows."""
    _check(table, fields, values=True, allow_hr=allow_hr)
    if not 1 <= top <= 100:
        raise AnalysisError("top must be between 1 and 100")
    rows, truncated = _cut(rows)
    counts = Counter(tuple(_cell(r.get(f)) for f in fields) for r in rows)
    total = len(rows)
    ordered = counts.most_common()
    out, run = [], 0
    cover: dict[str, int] = {}
    for i, (k, n) in enumerate(ordered, start=1):
        run += n
        for c in COVER:
            if str(c) not in cover and total and run * 100 >= c * total:
                cover[str(c)] = i
        if i <= top:
            out.append({"values": dict(zip(fields, k)), "rows": n, "share": round(n / total, 4) if total else 0, "cumulative": round(run / total, 4) if total else 0})
    shown = sum(g["rows"] for g in out)
    return {"table": table, "fields": fields, "rows_read": total, "truncated": truncated, "combinations": len(ordered), "top": out,
            "other": {"combinations": max(len(ordered) - len(out), 0), "rows": total - shown}, "cover": cover}


def selectivity(table: str, rows: list[dict], fields: list[str], allow_hr: bool = False) -> dict:
    """How selective a field combination is (DB05 style): distinct values against rows, the most repeated value, and a verdict. Values are shown
    only for fields that are not personal data."""
    _check(table, fields, values=False, allow_hr=allow_hr)
    rows, truncated = _cut(rows)
    counts = Counter(tuple(_cell(r.get(f)) for f in fields) for r in rows)
    total, distinct = len(rows), len(counts)
    ratio = round(distinct / total, 4) if total else 0.0
    hide = any(is_sensitive(table, f) for f in fields)
    most = counts.most_common(5)
    return {"table": table, "fields": fields, "rows_read": total, "truncated": truncated, "distinct": distinct, "selectivity": ratio,
            "max_duplicates": most[0][1] if most else 0, "average_duplicates": round(total / distinct, 2) if distinct else 0,
            "verdict": "unique-like" if ratio >= 0.9 else "selective" if ratio >= 0.2 else "low selectivity",
            "most_repeated": None if hide else [{"values": dict(zip(fields, k)), "rows": n} for k, n in most],
            "values_withheld": hide}


def growth(table: str, rows: list[dict], date_field: str, period: str = "month", allow_hr: bool = False) -> dict:
    """Rows per creation period (DB02 style growth, but for the data itself): counts, running total and change against the previous period."""
    _check(table, [date_field], values=False, allow_hr=allow_hr)
    if date_field not in DATE_FIELDS:
        raise AnalysisError(f"{date_field} is not a date field the platform recognises")
    if period not in ("month", "year"):
        raise AnalysisError("period must be month or year")
    width = 7 if period == "month" else 4
    rows, truncated = _cut(rows)
    counts: Counter = Counter()
    undated = 0
    for r in rows:
        v = str(r.get(date_field) or "")
        if len(v) >= width and v[:4].isdigit():
            counts[v[:width]] += 1
        else:
            undated += 1
    keys = sorted(counts)
    series, run, prev = [], 0, None
    for k in keys:
        n = counts[k]
        run += n
        series.append({"period": k, "rows": n, "cumulative": run, "change": None if prev in (None, 0) else round((n - prev) / prev, 3)})
        prev = n
    avg = round(sum(counts.values()) / len(keys), 2) if keys else 0
    return {"table": table, "date_field": date_field, "period": period, "rows_read": len(rows), "truncated": truncated, "undated": undated,
            "periods": len(keys), "average_per_period": avg, "busiest": max(series, key=lambda s: s["rows"]) if series else None, "series": series}


def table_objects(registry) -> list[dict]:
    """Which business object types of this platform carry each table (DB15 style), and which tables belong to none."""
    owners: dict[str, list[str]] = {}
    for name, ot in registry.types.items():
        for link in ot.tables:
            owners.setdefault(link.table, []).append(name)
    out = []
    for t, td in sorted(TABLES.items()):
        out.append({"table": t, "description": td.description, "customizing": td.config, "fields": list(td.fields), "objects": sorted(set(owners.get(t, []))),
                    "personal_fields": sorted(f for f in td.fields if is_sensitive(t, f))})
    return out


def profiles(family_tables: set[str] | None = None) -> dict:
    """Ready-made analyses that exist for the tables of this platform's data model."""
    cand = [("Sales orders by order type and sales organisation", "VBAK", ["AUART", "VKORG"]), ("Sales order items by plant", "VBAP", ["WERKS"]),
            ("Production and maintenance orders by order type", "AUFK", ["AUART", "WERKS"]), ("Accounting documents by document type", "BKPF", ["BLART", "BUKRS"]),
            ("Material movements by movement type", "MSEG", ["BWART"]), ("Material movements by movement type (S/4HANA)", "MATDOC", ["BWART"]),
            ("Purchase orders by type", "EKKO", ["BSART"]), ("Maintenance notifications by type", "QMEL", ["QMART", "SWERK"]),
            ("Inspection lots by type and plant", "QALS", ["ART", "WERKS"]), ("Equipment by class", "EQUI", ["EQART", "SWERK"]),
            ("Project costs by cost element and year", "COSP", ["KSTAR", "GJAHR"])]
    dist = [{"label": lbl, "table": t, "fields": f} for lbl, t, f in cand
            if t in TABLES and all(x in TABLES[t].fields for x in f) and (family_tables is None or t in family_tables)]
    grow = []
    for t, td in sorted(TABLES.items()):
        if td.config or t in HR_TABLES or (family_tables is not None and t not in family_tables):
            continue
        for f in td.fields:
            if f in DATE_FIELDS:
                grow.append({"table": t, "date_field": f})
                break
    return {"distribution": dist, "growth": grow}


NOT_AVAILABLE = [
    {"function": "Transaction profile of users (ST03N)", "reason": "runtime statistics are not table data; they need the workload collector"},
    {"function": "Single business transaction trace (STAD)", "reason": "runtime statistics are not table data"},
    {"function": "Workflow frequencies and durations (SWI2_FREQ)", "reason": "workflow logs are not part of the platform's data model"},
    {"function": "Interface message monitors (WE02, BD87)", "reason": "IDoc tables are not part of the platform's data model"},
    {"function": "Database size and growth in bytes (DB02)", "reason": "the platform sees rows, not the database's storage; growth is shown in rows per creation period"},
]

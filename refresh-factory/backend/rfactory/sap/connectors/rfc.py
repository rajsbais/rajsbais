"""Read-only SAP adapter over RFC (RFC_READ_TABLE + DDIF_FIELDINFO_GET).

IMPORTANT STATUS: this code is exercised only against `FakeRfcTransport` (fake_rfc.py), which models the documented behaviour and
limits of RFC_READ_TABLE. It has NEVER been run against a real SAP system, and the real transport (`PyRfcTransport`, needs the SAP
NetWeaver RFC SDK and pyrfc) is untested. Treat it as a carefully specified starting point, not as validated connectivity.

Design points that come from RFC_READ_TABLE's real constraints:
  * rows are limited to 512 characters: wide tables are read in field groups (every group repeats the key fields) and merged by key
  * WHERE clauses travel as 72-character lines: they are split only at token boundaries, never inside a literal
  * there is no ORDER BY, so paging by ROWSKIPS is only safe on a quiesced source: duplicates are detected and raised, and groups that
    return different key sets are refused
  * only an allow-list of function modules can be called (a bug can never reach a BAPI that writes)
  * every scan is bounded (`max_scan_rows`); calls are throttled and transient errors retried with backoff
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Protocol

from ..adapter import ChangeLogGap, Row, SapSystem
from ..ddic import TABLES
from .profile import ConnectionProfile, resolve_secret

ALLOWED_FUNCTIONS = frozenset({"RFC_READ_TABLE", "DDIF_FIELDINFO_GET", "RFC_SYSTEM_INFO", "RFC_PING"})
LINE = 72
ROW_BUDGET = 512


class RemoteError(RuntimeError):
    pass


class RfcCommunicationError(RemoteError):
    """Network/gateway problem: retried."""


class RemoteAuthError(RemoteError):
    pass


class RemoteTableMissing(RemoteError):
    pass


class ScanTooLarge(RemoteError):
    pass


class UnstablePaging(RemoteError):
    pass


class SourceChangedDuringRead(RemoteError):
    pass


class FunctionNotAllowed(PermissionError):
    pass


class RfcTransport(Protocol):
    def call(self, function: str, **params) -> dict: ...


class PyRfcTransport:
    """Real transport through pyrfc. UNTESTED: needs the SAP NW RFC SDK and a reachable system."""

    def __init__(self, profile: ConnectionProfile):
        import pyrfc  # optional dependency
        self._conn = pyrfc.Connection(ashost=profile.ashost, sysnr=profile.sysnr, client=profile.client, user=profile.user,
                                      passwd=resolve_secret(profile.password_ref), timeout=int(profile.timeout_s))
        self._errors = pyrfc

    def call(self, function: str, **params) -> dict:
        try:
            return self._conn.call(function, **params)
        except self._errors.CommunicationError as e:
            raise RfcCommunicationError(str(e))
        except self._errors.ABAPApplicationError as e:
            key = getattr(e, "key", "")
            if key == "NOT_AUTHORIZED":
                raise RemoteAuthError(str(e))
            if key in ("TABLE_NOT_AVAILABLE", "TABLE_WITHOUT_DATA"):
                raise RemoteTableMissing(str(e))
            raise RemoteError(f"{key}: {e}")


class AllowListTransport:
    """Refuses every function module that is not on the read-only allow-list."""

    def __init__(self, inner: RfcTransport, allowed=ALLOWED_FUNCTIONS):
        self._inner, self._allowed = inner, allowed

    def call(self, function: str, **params) -> dict:
        if function not in self._allowed:
            raise FunctionNotAllowed(f"function module {function} is not on the read-only allow-list")
        return self._inner.call(function, **params)


@dataclass
class Stats:
    calls: int = 0
    retries: int = 0
    rows: int = 0
    scans: int = 0
    scanned: list = field(default_factory=list)
    guard_trips: int = 0
    tables: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"calls": self.calls, "retries": self.retries, "rows": self.rows, "scans": self.scans, "scanned_tables": sorted(set(self.scanned)), "guard_trips": self.guard_trips, "tables": dict(self.tables)}


# ---------------------------------------------------------------- where clauses
def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def wrap_lines(tokens: list[str]) -> list[dict]:
    """Greedy split of a token list into OPTIONS lines (<= 72 chars, each ending in a blank), never splitting a token."""
    lines, cur = [], ""
    for t in tokens:
        piece = t + " "
        if len(piece) > LINE:
            raise RemoteError(f"a single WHERE token is longer than {LINE} characters: {t[:30]}...")
        if len(cur) + len(piece) > LINE:
            lines.append({"TEXT": cur})
            cur = ""
        cur += piece
    if cur:
        lines.append({"TEXT": cur})
    return lines


class Meta:
    """DDIC metadata of one table as DDIF_FIELDINFO_GET reports it."""

    def __init__(self, table: str, dfies: list[dict]):
        self.table = table
        self.fields = {d["FIELDNAME"]: d for d in dfies}
        self.order = [d["FIELDNAME"] for d in sorted(dfies, key=lambda d: int(d["POSITION"]))]
        self.keys = [f for f in self.order if self.fields[f].get("KEYFLAG") == "X"]

    def width(self, f: str) -> int:
        return int(self.fields[f]["LENG"])

    def external(self, f: str, value: Any) -> str:
        """Internal (ISO / python) value -> literal as it appears in a WHERE clause."""
        d = self.fields[f]["DATATYPE"]
        if d == "DATS":
            return str(value).replace("-", "") if value else "00000000"
        return str(value)

    def internal(self, f: str, raw: str) -> Any:
        d, dec = self.fields[f]["DATATYPE"], int(self.fields[f].get("DECIMALS") or 0)
        s = raw.strip() if d not in ("CHAR", "NUMC", "LANG", "CUKY", "UNIT") else raw.rstrip()
        if d == "DATS":
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if s and s != "00000000" else ""
        if d in ("CHAR", "NUMC", "LANG", "CUKY", "UNIT", "CLNT", "SSTR", "STRG"):
            return s
        if d in ("INT1", "INT2", "INT4", "INT8"):
            return int(self._num(s))
        if d in ("DEC", "CURR", "QUAN", "FLTP", "D16D", "D34D"):
            v = float(self._num(s)) if s else 0.0
            return int(v) if dec == 0 else v
        return s

    @staticmethod
    def _num(s: str) -> str:
        s = s.strip()
        if not s:
            return "0"
        return "-" + s[:-1].strip() if s.endswith("-") else s


class RfcSourceAdapter:
    """SourceAdapter over RFC. Only read methods exist; there is nothing to write with."""

    kind = "rfc"

    def __init__(self, system: SapSystem, transport: RfcTransport, profile: ConnectionProfile | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 reference: Callable[[], date] = date.today):
        self.system = system
        self.profile = profile or ConnectionProfile(system.sid, "rfc", ashost="-", client=system.client)
        self._t = AllowListTransport(transport)
        self._sleep, self._clock, self._reference = sleep, clock, reference
        self._meta: dict[str, Meta] = {}
        self.stats = Stats()
        self._last_call = 0.0
        self.drift: dict[str, list[str]] = {}
        cd = self.profile.options.get("change_documents")
        self.cdr = None
        if cd:  # opt-in: reading CDHDR needs authorisations the connecting user may not have
            from .changedocs import ChangeDocReader
            self.cdr = ChangeDocReader(self, **{k: v for k, v in (cd if isinstance(cd, dict) else {}).items() if k in ("lag_seconds", "overlap_seconds", "retention_days")})
        self.cd_error: str | None = None

    # ------------------------------------------------------------ transport with throttle and retry
    def _call(self, function: str, **params) -> dict:
        gap = 60.0 / max(1, self.profile.calls_per_minute)
        wait = gap - (self._clock() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        last: Exception | None = None
        for attempt in range(3):
            self._last_call = self._clock()
            self.stats.calls += 1
            try:
                return self._t.call(function, **params)
            except RfcCommunicationError as e:
                last = e
                self.stats.retries += 1
                self._sleep(0.5 * 2 ** attempt)
        raise RfcCommunicationError(f"{function} failed after 3 attempts: {last}")

    # ------------------------------------------------------------ metadata
    def meta(self, table: str) -> Meta:
        if table not in self._meta:
            r = self._call("DDIF_FIELDINFO_GET", TABNAME=table)
            if not r.get("DFIES_TAB"):
                raise RemoteTableMissing(f"table {table} is not known in the remote DDIC")
            m = Meta(table, r["DFIES_TAB"])
            self._meta[table] = m
            td = TABLES.get(table)
            if td:
                missing = [f for f in td.fields if f not in m.fields]
                keys_differ = [] if list(td.keys) == m.keys else [f"key {m.keys} differs from the model's {list(td.keys)}"]
                if missing or keys_differ:
                    self.drift[table] = [f"field {f} missing in the remote system" for f in missing] + keys_differ
        return self._meta[table]

    def schema_drift(self, tables=None) -> dict[str, list[str]]:
        """Compare the platform's reduced DDIC model with the remote system's. Empty = every modelled field exists with the same key."""
        for t in tables or [t for t, d in TABLES.items() if not d.config or t in ("T001", "T001W")]:
            try:
                self.meta(t)
            except RemoteTableMissing:
                self.drift[t] = ["table missing in the remote system"]
        return dict(self.drift)

    # ------------------------------------------------------------ reading
    def _groups(self, meta: Meta, fields: list[str]) -> list[list[str]]:
        keys = meta.keys
        base = sum(meta.width(k) for k in keys)
        if base > ROW_BUDGET:
            raise RemoteError(f"the key of {meta.table} alone is wider than the {ROW_BUDGET}-character RFC_READ_TABLE row")
        groups, cur, width = [], list(keys), base
        for f in fields:
            if f in keys:
                continue
            w = meta.width(f)
            if w > ROW_BUDGET - base:
                raise RemoteError(f"field {meta.table}-{f} ({w}) cannot be read together with the key through RFC_READ_TABLE")
            if width + w > ROW_BUDGET:
                groups.append(cur)
                cur, width = list(keys), base
            cur.append(f)
            width += w
        groups.append(cur)
        return groups

    def _page(self, table: str, meta: Meta, group: list[str], options: list[dict], limit: int) -> list[Row]:
        out, skip, page = [], 0, self.profile.page_rows
        seen: set[tuple] = set()
        while True:
            r = self._call("RFC_READ_TABLE", QUERY_TABLE=table, DELIMITER="", ROWSKIPS=skip, ROWCOUNT=page,
                           FIELDS=[{"FIELDNAME": f} for f in group], OPTIONS=options)
            layout = {f["FIELDNAME"]: (int(f["OFFSET"]), int(f["LENGTH"])) for f in r["FIELDS"]}
            data = r.get("DATA", [])
            for d in data:
                wa = d["WA"]
                row = {f: meta.internal(f, wa[o:o + n]) for f, (o, n) in layout.items()}
                k = tuple(row[f] for f in meta.keys)
                if k in seen:
                    raise UnstablePaging(f"{table}: key {k} returned twice while paging: the source is changing or its order is not stable; "
                                         "read from a quiesced source")
                seen.add(k)
                out.append(row)
            if len(out) > limit:
                self.stats.guard_trips += 1
                raise ScanTooLarge(f"{table}: more than {limit} rows match; narrow the selection (max_scan_rows={self.profile.max_scan_rows})")
            if len(data) < page:
                return out
            skip += page

    def _fetch(self, table: str, tokens: list[str] | None, fields: list[str] | None = None) -> list[Row]:
        meta = self.meta(table)
        td = TABLES.get(table)
        want = [f for f in (fields or (list(td.fields) if td else meta.order)) if f in meta.fields]
        options = wrap_lines(tokens) if tokens else []
        groups = self._groups(meta, want)
        first = self._page(table, meta, groups[0], options, self.profile.max_scan_rows)
        merged = {tuple(r[k] for k in meta.keys): dict(r) for r in first}
        for g in groups[1:]:
            more = self._page(table, meta, g, options, self.profile.max_scan_rows)
            if {tuple(r[k] for k in meta.keys) for r in more} != set(merged):
                raise SourceChangedDuringRead(f"{table}: the field groups returned different rows; the source changed while it was read")
            for r in more:
                merged[tuple(r[k] for k in meta.keys)].update(r)
        rows = [{f: merged[k].get(f, "") for f in (list(td.fields) if td else meta.order)} for k in merged]
        self.stats.rows += len(rows)
        self.stats.tables[table] = self.stats.tables.get(table, 0) + len(rows)
        return rows

    def _cond_eq(self, meta: Meta, field_: str, value) -> list[str]:
        return [field_, "=", _q(meta.external(field_, value))]

    # ------------------------------------------------------------ SourceAdapter (read side)
    def reference_date(self) -> date:
        return self._reference()

    def get(self, table: str, key: tuple) -> Row | None:
        meta = self.meta(table)
        toks: list[str] = []
        for f, v in zip(meta.keys, key):
            toks += (["AND"] if toks else []) + self._cond_eq(meta, f, v)
        rows = self._fetch(table, toks)
        return rows[0] if rows else None

    def lookup(self, table: str, field_: str, value) -> list[Row]:
        meta = self.meta(table)
        if field_ not in meta.fields:
            raise RemoteError(f"{table} has no field {field_}")
        return self._fetch(table, self._cond_eq(meta, field_, value))

    def select_in(self, table: str, field_: str, values) -> list[Row]:
        """WHERE field IN (...): pushed to the system, in batches so no single clause gets unwieldy."""
        meta, vals, out, seen = self.meta(table), sorted({str(v) for v in values}), [], set()
        for i in range(0, len(vals), 100):
            toks = [field_, "IN", "("] + [_q(meta.external(field_, v)) + ("," if j < len(vals[i:i + 100]) - 1 else "") for j, v in enumerate(vals[i:i + 100])] + [")"]
            for r in self._fetch(table, toks):
                k = tuple(r[f] for f in meta.keys)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
        return out

    def select_between(self, table: str, field_: str, lo: str, hi: str) -> list[Row]:
        meta = self.meta(table)
        return self._fetch(table, [field_, "BETWEEN", _q(meta.external(field_, lo)), "AND", _q(meta.external(field_, hi))])

    def select(self, table: str, predicate: Callable[[Row], bool] | None = None) -> list[Row]:
        """Full scan (bounded by max_scan_rows), filtered here. Prefer select_in / select_between / lookup, which are pushed down."""
        self.stats.scans += 1
        self.stats.scanned.append(table)
        rows = self._fetch(table, None)
        return [r for r in rows if predicate is None or predicate(r)]

    def count(self, table: str) -> int:
        self.stats.scans += 1
        meta = self.meta(table)
        return len(self._fetch(table, None, fields=list(meta.keys)))

    def table_counts(self) -> dict[str, int]:
        out = {}
        for t in TABLES:
            try:
                n = self.count(t)
            except ScanTooLarge:
                n = self.profile.max_scan_rows  # capped: a lower bound, flagged by guard_trips
            except RemoteTableMissing:
                continue
            if n:
                out[t] = n
        return out

    def discover(self) -> dict:
        info = self._call("RFC_SYSTEM_INFO").get("RFCSI_EXPORT", {})
        return {"simulated": False, "system": self.system.model_dump(mode="json"),
                "company_codes": [{"code": r["BUKRS"], "name": r["BUTXT"], "currency": r["WAERS"]} for r in self.select("T001")],
                "plants": [{"plant": r["WERKS"], "name": r["NAME1"], "company_code": r["BUKRS"]} for r in self.select("T001W")],
                "table_counts": {}, "custom_fields": [], "installed_components": [{"name": "SAP_BASIS", "release": info.get("RFCSAPRL", "?")}],
                "remote": {"kind": "rfc", "host": info.get("RFCHOST", ""), "database": info.get("RFCDBSYS", ""), "stats": self.stats.public()}}

    # change documents: read through CDHDR when the profile opts in (see changedocs.py); otherwise the delta engine compares by content
    def change_seq(self) -> int:
        if self.cdr is None:
            return 0
        try:
            self.cd_error = None
            return self.cdr.watermark()
        except Exception as e:  # noqa: BLE001 - the engine then finds no usable watermark and falls back to a full sweep
            self.cd_error = f"{type(e).__name__}: {e}"
            return 0

    def changes_since(self, seq: int) -> list[dict]:
        if self.cdr is None:
            raise ChangeLogGap("change documents are not enabled for this connection (profile option change_documents); a full compare is needed")
        if not seq or seq <= 0:
            raise ChangeLogGap(self.cd_error or "no change-document watermark is available")
        try:
            return self.cdr.read(seq)
        except ChangeLogGap:
            raise
        except RemoteError as e:
            raise ChangeLogGap(f"change documents could not be read ({e})") from e

    def change_coverage(self) -> set[str] | None:
        """Header tables whose changes the change documents report. None = change documents are not used (everything is compared)."""
        return set(self.cdr.coverage()) if self.cdr is not None else None

    def capabilities(self) -> dict:
        return {"pushdown": ["lookup", "get", "select_in", "select_between"], "full_scan": f"bounded to {self.profile.max_scan_rows} rows",
                "change_documents": self.cdr is not None, "writes": False, "validated_against_real_sap": False}


class DisconnectedAdapter:
    """Stands in for a remote system after a restart when the connection could not be re-established (the transport is never persisted)."""

    def __init__(self, system: SapSystem, profile: ConnectionProfile, reason: str):
        self.system, self.profile, self.reason = system, profile, reason
        self.kind = profile.kind
        self.drift: dict = {"connection": [reason]}
        self.stats = Stats()

    def _down(self, *a, **k):
        raise RemoteError(f"{self.system.label} is disconnected ({self.reason}); reconnect it before use")

    select = lookup = get = count = table_counts = discover = reference_date = change_seq = changes_since = change_coverage = select_in = select_between = gaps = _down

    def capabilities(self) -> dict:
        return {"connected": False, "reason": self.reason}

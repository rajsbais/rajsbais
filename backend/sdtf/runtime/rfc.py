"""RFC transport for the SAP add-on contract (sap-abap/README.md).

Three layers:
* `RfcTransport` - anything with `call(function_name, **params) -> dict` using RFC parameter names
  (IV_TABLE, IT_PREDICATE, ET_ROWS, ...). `PyRfcTransport` binds to SAP's NW RFC SDK through `pyrfc`;
  `SimulatedAbapAddon` is a Python implementation of the add-on's function modules over a `RecordStore`.
* `AbapAddonClient` - typed calls on top of a transport: open snapshot, table metadata, package reads with
  checksum verification and keyset cursors.
* `resolve_destination` - RFC destination parameters for a system, secrets taken from the environment.

The simulated add-on is the executable specification of the contract: every behaviour it enforces (snapshot
validity, S_TABU_NAM-style authorization, predicate semantics, package cap, primary-key ordering, checksum) is
what the ABAP reference implementation in `sap-abap/src/` must provide. It involves no SAP system.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Iterator, Protocol

from ..catalog.tables import TABLES, record_key
from . import rfc_config

# ------------------------------------------------------------------------------------------------ contract constants
FM_OPEN_SNAPSHOT = "Z_SDTF_OPEN_SNAPSHOT"
FM_TABLE_METADATA = "Z_SDTF_TABLE_METADATA"
FM_READ_PACKAGE = "Z_SDTF_READ_PACKAGE"
FM_CDC_POLL = "Z_SDTF_CDC_POLL"
FM_AGGREGATE = "Z_SDTF_AGGREGATE"
ABAP_TRUE, ABAP_FALSE = "X", ""
POSITIVE_OPS = ("EQ", "BT", "GE", "GT", "LE", "LT", "CP")
NEGATIVE_OPS = ("NE", "NB", "NP")
SERVER_MAX_PACKAGE = 10000  # the add-on clamps IV_PACKAGE to this
DDIC_TABLES = ("DD02L", "DD02T", "DD03L")  # answered from the catalogue by the simulator when the store has no such rows


class RfcError(Exception):
    """An ABAP exception of the add-on (key as in the function module's EXCEPTIONS) or a transport failure."""

    def __init__(self, key: str, message: str = ""):
        super().__init__(f"{key}: {message}" if message else key)
        self.key, self.message = key, message


class RfcIntegrityError(RfcError):
    def __init__(self, message: str):
        super().__init__("CHECKSUM_MISMATCH", message)


class RfcUnavailable(RfcError):
    def __init__(self, message: str):
        super().__init__("RFC_UNAVAILABLE", message)


class RfcTransport(Protocol):
    name: str

    def call(self, function_name: str, **params: Any) -> dict: ...


def row_json(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def package_checksum(json_rows: list[str]) -> str:
    """SHA-256 over the row JSON strings exactly as transmitted, joined by '\\n' (so field order on the ABAP side is irrelevant)."""
    return hashlib.sha256("\n".join(json_rows).encode("utf-8")).hexdigest()


# -------------------------------------------------------------------------------------------------------- predicates
def predicate(field_name: str, op: str, low: Any, high: Any = None) -> dict:
    op = op.upper()
    if op not in POSITIVE_OPS + NEGATIVE_OPS:
        raise ValueError(f"unsupported predicate op {op}")
    return {"FIELD": field_name.upper(), "OP": op, "LOW": "" if low is None else str(low), "HIGH": "" if high is None else str(high)}


def _match_one(value: str, p: dict) -> bool:
    op, low, high = p["OP"], p["LOW"], p.get("HIGH", "")
    if op in ("EQ", "NE"):
        r = value == low
    elif op in ("BT", "NB"):
        r = low <= value <= high
    elif op == "GE":
        r = value >= low
    elif op == "GT":
        r = value > low
    elif op == "LE":
        r = value <= low
    elif op == "LT":
        r = value < low
    elif op in ("CP", "NP"):
        r = fnmatchcase(value, low.replace("+", "?"))
    else:
        raise RfcError("INVALID_PREDICATE", f"op {op}")
    return (not r) if op in NEGATIVE_OPS else r


def predicates_match(row: dict, preds: list[dict]) -> bool:
    """SAP range-table semantics: predicates on the same field - positive ones OR-ed, negative ones AND-ed
    (exclusions); different fields AND-ed. An empty list matches everything."""
    by_field: dict[str, list[dict]] = {}
    for p in preds:
        by_field.setdefault(p["FIELD"], []).append(p)
    for f, ps in by_field.items():
        v = "" if row.get(f) is None else str(row.get(f))
        pos = [p for p in ps if p["OP"] in POSITIVE_OPS]
        neg = [p for p in ps if p["OP"] in NEGATIVE_OPS]
        if pos and not any(_match_one(v, p) for p in pos):
            return False
        if neg and not all(_match_one(v, p) for p in neg):
            return False
    return True


def _pred_hash(preds: list[dict]) -> str:
    return hashlib.sha1(json.dumps(preds, sort_keys=True).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------------------------- simulated ABAP add-on
@dataclass
class AddonStats:
    calls: int = 0
    packages: int = 0
    rows_served: int = 0
    max_package: int = 0
    tables: dict[str, int] = field(default_factory=dict)
    full_scans: int = 0  # reads without any predicate


class SimulatedAbapAddon:
    """Python implementation of the add-on's RFC-enabled function modules over an in-memory RecordStore.

    Faithful to the contract, not to SAP internals: it validates the snapshot token, enforces a table allow-list
    (the equivalent of S_TABU_NAM), pushes predicates down, orders by primary key, pages with an opaque keyset
    cursor, caps the package size and signs every package with a checksum. Used in tests, demos and as the
    reference behaviour for the ABAP implementation."""

    name = "SIMULATED_ADDON"

    def __init__(self, store, allowed_tables: set[str] | None = None, snapshot_ttl: float = 3600.0, server_max_package: int = SERVER_MAX_PACKAGE, clock=None, change_log: list[dict] | None = None):
        self.store = store
        self.change_log = sorted(change_log or [], key=lambda e: int(e["SEQ"]))  # the source's change log (see runtime/activity.py)
        self.allowed = {t.upper() for t in allowed_tables} if allowed_tables is not None else None
        self.snapshot_ttl = snapshot_ttl
        self.server_max_package = server_max_package
        self._clock = clock or time.time
        self._snapshots: dict[str, float] = {}
        self._sorted: dict[str, list[tuple[tuple[str, ...], dict]]] = {}
        self._lock = threading.Lock()
        self.stats = AddonStats()

    # -- helpers
    def _authorize(self, table: str) -> None:
        if table not in TABLES and not (table.startswith("Z") or table.startswith("Y")):
            raise RfcError("TABLE_UNKNOWN", f"table {table} does not exist in the DDIC")
        if self.allowed is not None and table not in self.allowed:
            raise RfcError("NOT_AUTHORIZED", f"no S_TABU_NAM authorization for {table} (activity 03)")

    def _check_snapshot(self, token: str) -> None:
        exp = self._snapshots.get(token)
        if exp is None:
            raise RfcError("SNAPSHOT_UNKNOWN", "open a snapshot with Z_SDTF_OPEN_SNAPSHOT first")
        if exp < self._clock():
            raise RfcError("SNAPSHOT_EXPIRED", "consistency token expired; open a new snapshot and restart the partition")

    def _key_tuple(self, table: str, row: dict) -> tuple[str, ...]:
        td = TABLES.get(table)
        keys = td.key_fields if td else sorted(row)
        return tuple("" if row.get(k) is None else str(row.get(k)) for k in keys)

    def _rows_sorted(self, table: str) -> list[tuple[tuple[str, ...], dict]]:
        if table not in self._sorted:
            self._sorted[table] = sorted(((self._key_tuple(table, r), r) for r in self._table_rows(table)), key=lambda kr: kr[0])
        return self._sorted[table]

    def _table_rows(self, table: str) -> list[dict]:
        """The store's rows, or, for the DDIC tables a store never carries, what an SAP system would answer: one
        DD02L / DD02T row per catalogued or present table and one DD03L row per field (the catalogue is the DDIC
        of the synthetic landscape)."""
        rows = self.store.rows(table)
        if rows or table not in DDIC_TABLES:
            return rows
        present = set(self.store.tables())
        names = sorted(set(TABLES) | {t for t in present if t.startswith(("Z", "Y"))})
        out = []
        for name in names:
            td = TABLES.get(name)
            if table == "DD02L":
                out.append({"TABNAME": name, "AS4LOCAL": "A", "AS4VERS": "0000", "TABCLASS": "TRANSP", "CONTFLAG": "A" if td and td.domain != "CONFIG" else "C", "SQLTAB": "", "DEVCLASS": "ZSDTF" if name.startswith(("Z", "Y")) else "SAPAPPL"})
            elif table == "DD02T":
                out.append({"TABNAME": name, "DDLANGUAGE": "E", "AS4LOCAL": "A", "AS4VERS": "0000", "DDTEXT": td.description if td else f"Custom table {name}"})
            else:
                fields = list(td.fields) if td else sorted({k for r in self.store.rows(name)[:50] for k in r})
                keys = set(td.key_fields) if td else set()
                for i, f in enumerate(fields, start=1):
                    out.append({"TABNAME": name, "FIELDNAME": f, "AS4LOCAL": "A", "AS4VERS": "0000", "POSITION": str(i).zfill(4), "KEYFLAG": "X" if f in keys else "", "ROLLNAME": f, "DATATYPE": "CHAR", "LENG": "000040"})
        return out

    @staticmethod
    def _encode_cursor(table: str, phash: str, last_key: tuple[str, ...], snapshot: str) -> str:
        return base64.urlsafe_b64encode(json.dumps({"t": table, "p": phash, "k": list(last_key), "s": snapshot}).encode()).decode()

    @staticmethod
    def _decode_cursor(cursor: str) -> dict:
        try:
            return json.loads(base64.urlsafe_b64decode(cursor.encode()))
        except Exception as e:  # noqa: BLE001
            raise RfcError("INVALID_CURSOR", "cursor is not one issued by this add-on") from e

    # -- function modules
    def call(self, function_name: str, **params: Any) -> dict:
        with self._lock:
            self.stats.calls += 1
        fm = {FM_OPEN_SNAPSHOT: self._open_snapshot, FM_TABLE_METADATA: self._table_metadata, FM_READ_PACKAGE: self._read_package, FM_CDC_POLL: self._cdc_poll, FM_AGGREGATE: self._aggregate}.get(function_name)
        if fm is None:
            raise RfcError("FU_NOT_FOUND", f"function module {function_name} does not exist")
        return fm(**{k.upper(): v for k, v in params.items()})

    def _open_snapshot(self, IT_TABLES: list | None = None, **_: Any) -> dict:
        token = "snap-" + secrets.token_hex(8)
        valid_until = self._clock() + self.snapshot_ttl
        self._snapshots[token] = valid_until
        tables = [t["TABNAME"] if isinstance(t, dict) else str(t) for t in (IT_TABLES or [])] or self.store.tables()
        wm = [{"TABNAME": t, "WATERMARK": f"rows={self.store.count(t)}"} for t in tables]
        cdc_wm = str(self.change_log[-1]["SEQ"]) if self.change_log else "0"
        return {"EV_SNAPSHOT": token, "EV_VALID_UNTIL": time.strftime("%Y%m%d%H%M%S", time.gmtime(valid_until)), "ET_WATERMARKS": wm, "EV_CDC_WATERMARK": cdc_wm}

    def _table_metadata(self, IV_TABLE: str = "", **_: Any) -> dict:
        table = IV_TABLE.upper()
        self._authorize(table)
        td = TABLES.get(table)
        fields = [{"FIELDNAME": f, "KEYFLAG": ABAP_TRUE if (td and f in td.key_fields) else ABAP_FALSE, "DATATYPE": "CHAR", "LENG": 0} for f in (td.fields if td else sorted({k for r in self.store.rows(table)[:50] for k in r}))]
        n = len(self._table_rows(table)) if table in DDIC_TABLES else self.store.count(table)
        return {"ET_FIELDS": fields, "EV_ROWS": n, "EV_SIZE_MB": round(n * (td.avg_row_bytes if td else 256) / 1048576, 3), "EV_TABCLASS": "TRANSP", "EV_AUTHORIZED": ABAP_TRUE}

    def _read_package(self, IV_TABLE: str = "", IT_PREDICATE: list | None = None, IV_PACKAGE: int = 1000, IV_CURSOR: str = "", IV_SNAPSHOT: str = "", **_: Any) -> dict:
        table = IV_TABLE.upper()
        self._authorize(table)
        self._check_snapshot(IV_SNAPSHOT)
        preds = [{"FIELD": p["FIELD"].upper(), "OP": p["OP"].upper(), "LOW": str(p.get("LOW", "")), "HIGH": str(p.get("HIGH", ""))} for p in (IT_PREDICATE or [])]
        for p in preds:
            if p["OP"] not in POSITIVE_OPS + NEGATIVE_OPS:
                raise RfcError("INVALID_PREDICATE", f"op {p['OP']} on {p['FIELD']}")
        phash = _pred_hash(preds)
        package = max(1, min(int(IV_PACKAGE or 1000), self.server_max_package))
        after: tuple[str, ...] | None = None
        if IV_CURSOR:
            c = self._decode_cursor(IV_CURSOR)
            if c.get("t") != table or c.get("p") != phash or c.get("s") != IV_SNAPSHOT:
                raise RfcError("INVALID_CURSOR", "cursor belongs to a different table, predicate or snapshot")
            after = tuple(c["k"])
        out: list[dict] = []
        last: tuple[str, ...] | None = None
        eof = True
        for key, row in self._rows_sorted(table):
            if after is not None and key <= after:
                continue
            if not predicates_match(row, preds):
                continue
            if len(out) == package:
                eof = False
                break
            out.append(row)
            last = key
        json_rows = [row_json(r) for r in out]
        with self._lock:
            self.stats.packages += 1
            self.stats.rows_served += len(out)
            self.stats.max_package = max(self.stats.max_package, len(out))
            self.stats.tables[table] = self.stats.tables.get(table, 0) + len(out)
            if not preds:
                self.stats.full_scans += 1
        return {"ET_ROWS": [{"ROWNO": i + 1, "JSON": j} for i, j in enumerate(json_rows)], "EV_CURSOR": self._encode_cursor(table, phash, last, IV_SNAPSHOT) if (last is not None and not eof) else "", "EV_EOF": ABAP_TRUE if eof else ABAP_FALSE, "EV_CHECKSUM": package_checksum(json_rows), "EV_ROWS": len(out)}

    def _aggregate(self, IV_TABLE: str = "", IT_PREDICATE: list | None = None, IT_GROUP_BY: list | None = None, IT_SUM: list | None = None, IV_SNAPSHOT: str = "", **_: Any) -> dict:
        """COUNT and SUM per group computed in the source (one SELECT ... GROUP BY on the ABAP side): the
        reconciliation reads totals without transferring rows and checks that its row reads were complete."""
        table = IV_TABLE.upper()
        self._authorize(table)
        self._check_snapshot(IV_SNAPSHOT)
        preds = [{"FIELD": p["FIELD"].upper(), "OP": p["OP"].upper(), "LOW": str(p.get("LOW", "")), "HIGH": str(p.get("HIGH", ""))} for p in (IT_PREDICATE or [])]
        for p in preds:
            if p["OP"] not in POSITIVE_OPS + NEGATIVE_OPS:
                raise RfcError("INVALID_PREDICATE", f"op {p['OP']} on {p['FIELD']}")
        group = [str(g["FIELDNAME"] if isinstance(g, dict) else g).upper() for g in (IT_GROUP_BY or [])]
        sums = [str(f["FIELDNAME"] if isinstance(f, dict) else f).upper() for f in (IT_SUM or [])]
        td = TABLES.get(table)
        known = set(td.fields) | set(td.key_fields) if td else None
        for f in group + sums:
            if known is not None and f not in known:
                raise RfcError("INVALID_FIELD", f"{f} is not a field of {table}")
        acc: dict[tuple, dict] = {}
        for _key, row in self._rows_sorted(table):
            if not predicates_match(row, preds):
                continue
            g = tuple("" if row.get(f) is None else str(row.get(f)) for f in group)
            a = acc.get(g)
            if a is None:
                a = acc[g] = {**{f: v for f, v in zip(group, g, strict=True)}, "COUNT": 0, **{f"SUM_{f}": 0.0 for f in sums}}
            a["COUNT"] += 1
            for f in sums:
                try:
                    a[f"SUM_{f}"] += float(row.get(f) or 0)
                except (TypeError, ValueError) as e:
                    raise RfcError("INVALID_FIELD", f"{f} of {table} is not numeric") from e
        out = [{**a, **{f"SUM_{f}": round(a[f"SUM_{f}"], 2) for f in sums}} for _g, a in sorted(acc.items())]
        if not group and not out:
            out = [{"COUNT": 0, **{f"SUM_{f}": 0.0 for f in sums}}]
        json_rows = [row_json(r) for r in out]
        with self._lock:
            self.stats.calls += 0
            self.stats.packages += 1
        return {"ET_ROWS": [{"ROWNO": i + 1, "JSON": j} for i, j in enumerate(json_rows)], "EV_CHECKSUM": package_checksum(json_rows), "EV_ROWS": len(out)}

    def _cdc_poll(self, IV_WATERMARK: str = "0", IT_OBJECTS: list | None = None, IV_PACKAGE: int = 1000, **_: Any) -> dict:
        """Change events after the watermark for the listed tables (range-table predicates per table, evaluated on the
        row image; deletes carry no image and pass the table filter). Watermark = last delivered sequence."""
        try:
            after = int(IV_WATERMARK or 0)
        except ValueError as e:
            raise RfcError("INVALID_WATERMARK", f"watermark {IV_WATERMARK!r} was not issued by this add-on") from e
        preds_by_table: dict[str, list[dict]] = {}
        for o in IT_OBJECTS or []:
            t = str(o.get("TABNAME", "")).upper()
            if not t:
                continue
            self._authorize(t)
            preds_by_table.setdefault(t, [])
            if o.get("FIELD"):
                preds_by_table[t].append({"FIELD": str(o["FIELD"]).upper(), "OP": str(o.get("OP", "EQ")).upper(), "LOW": str(o.get("LOW", "")), "HIGH": str(o.get("HIGH", ""))})
        package = max(1, min(int(IV_PACKAGE or 1000), self.server_max_package))
        out: list[dict] = []
        last = after
        eof = True
        for e in self.change_log:
            if int(e["SEQ"]) <= after:
                continue
            t = e["TABNAME"]
            if preds_by_table and t not in preds_by_table:
                last = int(e["SEQ"])  # filtered events still advance the watermark
                continue
            if e["OP"] != "D" and preds_by_table.get(t) and not predicates_match(json.loads(e["JSON"]), preds_by_table[t]):
                last = int(e["SEQ"])
                continue
            if len(out) == package:
                eof = False
                break
            out.append({k: e[k] for k in ("SEQ", "CHANGENR", "OBJECT_TYPE", "TABNAME", "KEY", "OP", "CHANGED_AT", "CHANGED_BY", "JSON")})
            last = int(e["SEQ"])
        json_events = [row_json(ev) for ev in out]
        with self._lock:
            self.stats.packages += 1
            self.stats.rows_served += len(out)
        return {"ET_EVENTS": out, "EV_WATERMARK": str(last), "EV_EOF": ABAP_TRUE if eof else ABAP_FALSE, "EV_CHECKSUM": package_checksum(json_events), "EV_EVENTS": len(out)}


# ------------------------------------------------------------------------------------------------------ pyrfc binding
class PyRfcTransport:
    """SAP NW RFC SDK binding through the `pyrfc` package (SAP-distributed; not on PyPI for recent releases:
    https://github.com/SAP/PyRFC). Calls are serialised on one connection. Not verified against a live SAP
    system in this repository."""

    name = "PYRFC"

    def __init__(self, destination: dict):
        try:
            import pyrfc  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RfcUnavailable("pyrfc (SAP NW RFC SDK) is not installed; see docs/02-sap-connectivity-design.md") from e
        self._pyrfc = pyrfc
        self._lock = threading.Lock()
        try:
            self._conn = pyrfc.Connection(**destination)
        except Exception as e:  # noqa: BLE001
            raise RfcError(type(e).__name__.upper(), str(e)) from e

    def call(self, function_name: str, **params: Any) -> dict:
        with self._lock:
            try:
                return self._conn.call(function_name, **params)
            except Exception as e:  # noqa: BLE001
                key = getattr(e, "key", None) or type(e).__name__.upper()
                raise RfcError(str(key), getattr(e, "message", None) or str(e)) from e

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


SECRET_KEYS = ("passwd", "password", "snc_myname", "x509cert")


def resolve_destination(sid: str, meta: dict | None = None) -> dict:
    """RFC destination parameters (pyrfc.Connection kwargs) for a system. Sources, in order: `meta.rfc.dest`
    (no secrets in the DB: values 'env:NAME' are resolved from the environment), then `SDTF_RFC_DEST_<SID>`
    (JSON) with the same convention, then `SDTF_RFC_DEST_<SID>_PASSWD` for the password."""
    rfc = (meta or {}).get("rfc", {}) if meta else {}
    dest = dict(rfc.get("dest") or {})
    if not dest:
        raw = os.getenv(f"SDTF_RFC_DEST_{sid.upper()}", "")
        dest = json.loads(raw) if raw else {}
    out = {}
    for k, v in dest.items():
        if isinstance(v, str) and v.startswith("env:"):
            v = os.getenv(v[4:], "")
        out[k] = v
    pw = os.getenv(f"SDTF_RFC_DEST_{sid.upper()}_PASSWD")
    if pw and not out.get("passwd"):
        out["passwd"] = pw
    return out


def mask_destination(dest: dict) -> dict:
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in dest.items()}


def make_transport(sid: str, meta: dict | None, store_loader=None, change_log_loader=None) -> RfcTransport:
    """Transport for a system: `meta.rfc.transport` = simulated | pyrfc, default from settings (auto: pyrfc when a
    destination exists, else an error that names the fix)."""
    rfc = (meta or {}).get("rfc", {}) or {}
    mode = (rfc.get("transport") or rfc_config.transport_mode()).lower()
    if mode == "simulated":
        if store_loader is None:
            raise RfcUnavailable("simulated add-on needs the system's record store")
        return SimulatedAbapAddon(store_loader(), allowed_tables=set(rfc["allowed_tables"]) if rfc.get("allowed_tables") else None, change_log=change_log_loader() if change_log_loader else None)
    dest = resolve_destination(sid, meta)
    if mode == "pyrfc" or (mode == "auto" and dest):
        if not dest:
            raise RfcUnavailable(f"no RFC destination for {sid}: set SDTF_RFC_DEST_{sid.upper()} (JSON pyrfc.Connection parameters) or meta.rfc.dest")
        return PyRfcTransport(dest)
    raise RfcUnavailable(f"no RFC destination configured for {sid}; set SDTF_RFC_DEST_{sid.upper()} or register the system with meta.rfc.transport='simulated'")


# ------------------------------------------------------------------------------------------------------------ client
class AbapAddonClient:
    """Typed access to the add-on through any transport. Verifies package checksums, drives keyset cursors."""

    def __init__(self, transport: RfcTransport, package_size: int | None = None):
        self.t = transport
        self.package_size = package_size or rfc_config.package_size()
        self.snapshot: str | None = None
        self.valid_until: str | None = None
        self.calls = 0
        self.packages = 0
        self.rows = 0
        self.last_watermark: str | None = None
        self.cdc_watermark: str | None = None

    def open_snapshot(self, tables: list[str] | None = None) -> str:
        self.calls += 1
        r = self.t.call(FM_OPEN_SNAPSHOT, IT_TABLES=[{"TABNAME": t} for t in (tables or [])])
        self.snapshot, self.valid_until = r["EV_SNAPSHOT"], r.get("EV_VALID_UNTIL")
        self.cdc_watermark = str(r.get("EV_CDC_WATERMARK", "")) or None  # where delta capture starts for this snapshot
        return self.snapshot

    def table_metadata(self, table: str) -> dict:
        self.calls += 1
        r = self.t.call(FM_TABLE_METADATA, IV_TABLE=table.upper())
        return {"table": table.upper(), "fields": [f["FIELDNAME"] for f in r.get("ET_FIELDS", [])], "key_fields": [f["FIELDNAME"] for f in r.get("ET_FIELDS", []) if f.get("KEYFLAG") == ABAP_TRUE], "rows": int(r.get("EV_ROWS", 0)), "size_mb": float(r.get("EV_SIZE_MB", 0)), "authorized": r.get("EV_AUTHORIZED") == ABAP_TRUE}

    def read_package(self, table: str, preds: list[dict], cursor: str = "") -> tuple[list[dict], str, bool]:
        if not self.snapshot:
            self.open_snapshot()
        self.calls += 1
        r = self.t.call(FM_READ_PACKAGE, IV_TABLE=table.upper(), IT_PREDICATE=preds, IV_PACKAGE=self.package_size, IV_CURSOR=cursor, IV_SNAPSHOT=self.snapshot)
        json_rows = [e["JSON"] for e in r.get("ET_ROWS", [])]
        expected = r.get("EV_CHECKSUM", "")
        actual = package_checksum(json_rows)
        if expected != actual:
            raise RfcIntegrityError(f"package of {table} failed checksum verification (expected {expected[:12]}…, got {actual[:12]}…)")
        rows = [json.loads(j) for j in json_rows]
        self.packages += 1
        self.rows += len(rows)
        return rows, r.get("EV_CURSOR", "") or "", r.get("EV_EOF") == ABAP_TRUE

    def read_all(self, table: str, preds: list[dict]) -> Iterator[dict]:
        cursor, eof = "", False
        while not eof:
            rows, cursor, eof = self.read_package(table, preds, cursor)
            yield from rows
            if not eof and not cursor:
                raise RfcError("INVALID_CURSOR", "add-on reported more data but returned no cursor")

    def cdc_poll(self, watermark: str, objects: list[dict], package: int | None = None) -> tuple[list[dict], str, bool]:
        """One Z_SDTF_CDC_POLL package: events (dicts with the contract's upper-case keys, JSON already parsed into
        `row`), the new watermark and eof. Checksum verified over the events as transmitted."""
        self.calls += 1
        r = self.t.call(FM_CDC_POLL, IV_WATERMARK=str(watermark), IT_OBJECTS=objects, IV_PACKAGE=package or self.package_size)
        events = r.get("ET_EVENTS", [])
        expected, actual = r.get("EV_CHECKSUM", ""), package_checksum([row_json(e) for e in events])
        if expected != actual:
            raise RfcIntegrityError(f"CDC package failed checksum verification (expected {expected[:12]}…, got {actual[:12]}…)")
        for e in events:
            e["row"] = json.loads(e["JSON"]) if e.get("JSON") else None
        self.packages += 1
        self.rows += len(events)
        return events, str(r.get("EV_WATERMARK", watermark)), r.get("EV_EOF") == ABAP_TRUE

    def cdc_events(self, watermark: str, objects: list[dict], max_packages: int | None = None) -> Iterator[dict]:
        """All events after the watermark; `self.last_watermark` holds the watermark to persist afterwards."""
        self.last_watermark = watermark
        eof, n = False, 0
        while not eof and (max_packages is None or n < max_packages):
            events, watermark, eof = self.cdc_poll(watermark, objects)
            self.last_watermark = watermark
            n += 1
            yield from events

    def aggregate(self, table: str, preds: list[dict], group_by: list[str], sums: list[str]) -> list[dict]:
        """Z_SDTF_AGGREGATE: one row per group with the group fields, COUNT and SUM_<field> per summed field;
        computed in the source database. Checksum verified like a package."""
        if not self.snapshot:
            self.open_snapshot()
        self.calls += 1
        r = self.t.call(FM_AGGREGATE, IV_TABLE=table.upper(), IT_PREDICATE=preds, IT_GROUP_BY=[{"FIELDNAME": g.upper()} for g in group_by], IT_SUM=[{"FIELDNAME": f.upper()} for f in sums], IV_SNAPSHOT=self.snapshot)
        json_rows = [e["JSON"] for e in r.get("ET_ROWS", [])]
        expected, actual = r.get("EV_CHECKSUM", ""), package_checksum(json_rows)
        if expected != actual:
            raise RfcIntegrityError(f"aggregate of {table} failed checksum verification (expected {expected[:12]}…, got {actual[:12]}…)")
        self.packages += 1
        return [json.loads(j) for j in json_rows]

    def count(self, table: str, preds: list[dict]) -> int:
        rows = self.aggregate(table, preds, [], [])
        return int(rows[0]["COUNT"]) if rows else 0

    def read_keyed(self, table: str, preds: list[dict]) -> dict[str, dict]:
        return {record_key(table, r): r for r in self.read_all(table, preds)} if table in TABLES else {row_json(r): r for r in self.read_all(table, preds)}

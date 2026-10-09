"""A fake RFC endpoint that serves a SimulatedSap through the documented behaviour and limits of RFC_READ_TABLE / DDIF_FIELDINFO_GET.

It exists so the RFC adapter's protocol logic (field grouping, WHERE-line splitting, paging, type conversion, error mapping, retries) can
be tested without SAP. It is a MODEL of RFC_READ_TABLE, written from its documented behaviour; it can be wrong about details a real
system would reveal. Row order is deterministic but unrelated to the key (like a database without ORDER BY).
"""
from __future__ import annotations

import random
import re
from typing import Any

from ..ddic import TABLES
from .changedocs import CLASSES
from .rfc import RemoteAuthError, RemoteError, RemoteTableMissing, RfcCommunicationError

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(<>|<=|>=|=|<|>|\(|\)|,)|([A-Za-z_][A-Za-z0-9_/]*))")


def _d(name, pos, key, dt, ln):
    return {"FIELDNAME": name, "POSITION": f"{pos:04d}", "KEYFLAG": "X" if key else "", "DATATYPE": dt, "LENG": str(ln), "DECIMALS": "0", "INTTYPE": "C"}


# system tables that are not part of the platform's DDIC model but are served for the change-document reader
EXTRA_DFIES = {
    "CDHDR": [_d("MANDANT", 1, 1, "CLNT", 3), _d("OBJECTCLAS", 2, 1, "CHAR", 15), _d("OBJECTID", 3, 1, "CHAR", 90), _d("CHANGENR", 4, 1, "NUMC", 10),
              _d("USERNAME", 5, 0, "CHAR", 12), _d("UDATE", 6, 0, "DATS", 8), _d("UTIME", 7, 0, "TIMS", 6), _d("TCODE", 8, 0, "CHAR", 20), _d("CHANGE_IND", 9, 0, "CHAR", 1)],
    "TCDOB": [_d("OBJECT", 1, 1, "CHAR", 15), _d("TABNAME", 2, 1, "CHAR", 30)],
}


class FakeRfcTransport:
    def __init__(self, sap, *, denied: set[str] | None = None, unstable: set[str] | None = None, widen: dict[str, int] | None = None, seed: int = 11,
                 unlogged_classes: set[str] | None = None, no_clock: bool = False):
        self.unlogged_classes, self.no_clock = unlogged_classes or set(), no_clock  # classes TCDOB does not list; a system that reports no date/time
        self.sap, self.denied, self.unstable = sap, denied or set(), unstable or set()
        self.widen = widen or {}  # table -> extra width added to its widest text field (to force field grouping)
        self.calls: list[dict] = []
        self._fail: list[Exception] = []
        self._rng = random.Random(seed)
        self._order: dict[str, list[int]] = {}
        self._meta_cache: dict[str, list[dict]] = {}

    # --- test controls
    def fail_next(self, n: int, exc: Exception | None = None) -> None:
        self._fail += [exc or RfcCommunicationError("gateway timeout")] * n

    # --- the RFC surface
    def call(self, function: str, **p) -> dict:
        self.calls.append({"function": function, **{k: v for k, v in p.items() if k != "FIELDS"}, "fields": [f["FIELDNAME"] for f in p.get("FIELDS", [])]})
        if self._fail:
            raise self._fail.pop(0)
        if function == "RFC_PING":
            return {}
        if function == "RFC_SYSTEM_INFO":
            s = self.sap.system
            now = getattr(self.sap, "sim_now", None)
            return {"RFCSI_EXPORT": {"RFCSYSID": s.sid, "RFCHOST": f"{s.sid.lower()}app01", "RFCSAPRL": s.release, "RFCDBSYS": s.db_type,
                                     **({"RFCDATE": now.strftime("%Y%m%d"), "RFCTIME": now.strftime("%H%M%S")} if now and not self.no_clock else {})}}
        if function == "DDIF_FIELDINFO_GET":
            return {"DFIES_TAB": self._dfies(p["TABNAME"])}
        if function == "RFC_READ_TABLE":
            return self._read(p)
        raise RemoteError(f"function {function} is not available in the fake")

    # --- metadata
    def _dfies(self, table: str) -> list[dict]:
        if table in self.denied:
            return self._dfies_raw(table)  # metadata is readable; data is not authorised
        return self._dfies_raw(table)

    def _dfies_raw(self, table: str) -> list[dict]:
        td = TABLES.get(table)
        if td is None and table not in EXTRA_DFIES:
            return []
        if table in EXTRA_DFIES:
            return EXTRA_DFIES[table]
        if table in self._meta_cache:
            return self._meta_cache[table]
        rows = self.sap.data.get(table, [])
        out = []
        for pos, f in enumerate(td.fields, start=1):
            vals = [r.get(f) for r in rows if r.get(f) not in (None, "")]
            sample = vals[0] if vals else ""
            if all(isinstance(v, str) and _DATE.match(v) for v in vals) and vals:
                dt, ln, dec = "DATS", 8, 0
            elif vals and all(isinstance(v, bool) is False and isinstance(v, int) for v in vals):
                dt, ln, dec = "INT4", 12, 0
            elif vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                dt, ln, dec = "CURR", 17, 2
            else:
                width = max([len(str(v)) for v in vals] + [len(f) if f else 1, 1])
                dt = "NUMC" if vals and all(str(v).isdigit() for v in vals) and f in td.keys else "CHAR"
                ln, dec = max(width, 1) + (1 if dt == "CHAR" else 0), 0
                if f in self.widen.get(table, {}):
                    ln = self.widen[table][f]
            out.append({"FIELDNAME": f, "POSITION": f"{pos:04d}", "KEYFLAG": "X" if f in td.keys else "", "DATATYPE": dt, "LENG": str(ln), "DECIMALS": str(dec),
                        "INTTYPE": "C"})
        self._meta_cache[table] = out
        return out

    # --- external formatting
    @staticmethod
    def _fmt(d: dict, v: Any) -> str:
        ln, dt, dec = int(d["LENG"]), d["DATATYPE"], int(d["DECIMALS"])
        if dt == "DATS":
            return (str(v).replace("-", "") if v else "00000000").ljust(ln)[:ln]
        if dt in ("INT4", "CURR"):
            x = float(v or 0)
            text = f"{abs(x):.{dec}f}" + ("-" if x < 0 else "")
            return text.rjust(ln)[-ln:]
        return str(v if v is not None else "").ljust(ln)[:ln]

    # --- where clause
    @staticmethod
    def _tokens(text: str) -> list[str]:
        toks, pos = [], 0
        while pos < len(text):
            if not text[pos:].strip():
                break
            m = _TOKEN.match(text, pos)
            if not m:
                raise RemoteError("OPTION_NOT_VALID")
            toks.append(m.group(1) or m.group(2) or m.group(3))
            pos = m.end()
        return toks

    def _compile(self, table: str, options: list[dict]):
        for o in options:
            if len(o["TEXT"]) > 72:
                raise RemoteError("OPTION_NOT_VALID: line longer than 72 characters")
        text = "".join(o["TEXT"] for o in options)  # literal concatenation: the strictest reading
        toks = self._tokens(text)
        fields = {d["FIELDNAME"]: d for d in self._dfies_raw(table)}
        i = 0

        def peek():
            return toks[i].upper() if i < len(toks) else None

        def take():
            nonlocal i
            t = toks[i]
            i += 1
            return t

        def lit():
            t = take()
            if not t.startswith("'"):
                raise RemoteError("OPTION_NOT_VALID: literal expected")
            return t[1:-1].replace("''", "'")

        def cond():
            nonlocal i
            if peek() == "(":
                take()
                e = orexp()
                if peek() != ")":
                    raise RemoteError("OPTION_NOT_VALID: missing )")
                take()
                return e
            name = take()
            if name not in fields:
                raise RemoteError("FIELD_NOT_VALID")
            op = peek()
            if op in ("=", "<>", "<", ">", "<=", ">="):
                take()
                val = lit()
                return lambda r, n=name, o=op, v=val: self._cmp(fields[n], r, o, v)
            if op == "IN":
                take()
                if take() != "(":
                    raise RemoteError("OPTION_NOT_VALID")
                vals = [lit()]
                while peek() == ",":
                    take()
                    vals.append(lit())
                if take() != ")":
                    raise RemoteError("OPTION_NOT_VALID")
                return lambda r, n=name, vs=tuple(vals): any(self._cmp(fields[n], r, "=", v) for v in vs)
            if op == "BETWEEN":
                take()
                lo = lit()
                if peek() != "AND":
                    raise RemoteError("OPTION_NOT_VALID")
                take()
                hi = lit()
                return lambda r, n=name, a=lo, b=hi: self._cmp(fields[n], r, ">=", a) and self._cmp(fields[n], r, "<=", b)
            raise RemoteError("OPTION_NOT_VALID")

        def andexp():
            e = cond()
            while peek() == "AND":
                take()
                e = (lambda a, b: lambda r: a(r) and b(r))(e, cond())
            return e

        def orexp():
            e = andexp()
            while peek() == "OR":
                take()
                e = (lambda a, b: lambda r: a(r) or b(r))(e, andexp())
            return e

        if not toks:
            return lambda r: True
        e = orexp()
        if i != len(toks):
            raise RemoteError("OPTION_NOT_VALID: trailing tokens")
        return e

    def _cmp(self, d: dict, row: dict, op: str, literal: str) -> bool:
        raw = self._fmt(d, row.get(d["FIELDNAME"]))
        if d["DATATYPE"] in ("INT4", "CURR"):
            a, b = float(raw.strip().replace(" ", "").rstrip("-") or 0) * (-1 if raw.strip().endswith("-") else 1), float(literal)
        else:
            a, b = raw.rstrip(), literal.rstrip()
        return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]

    # --- change documents served from the simulated change log
    def _rows(self, table: str) -> list[dict]:
        if table == "TCDOB":
            return [{"OBJECT": c, "TABNAME": t} for c, (_h, tabs) in CLASSES.items() if c not in self.unlogged_classes for t in tabs]
        if table != "CDHDR":
            return self.sap.data.get(table, [])
        now = getattr(self.sap, "sim_now", None)
        owner = {t: c for c, (_h, tabs) in CLASSES.items() for t in tabs if t != "ADRC"}
        out = []
        for e in self.sap.changelog:
            if not e.get("ts") or not now or e["visible_at"] > now.isoformat():
                continue  # written but not committed yet: invisible to other sessions
            targets = []
            if e["table"] == "ADRC":
                for t, f, cls in (("KNA1", "KUNNR", "DEBI"), ("LFA1", "LIFNR", "KRED")):
                    targets += [(cls, t, {f: r[f]}) for r in self.sap.data.get(t, []) if r.get("ADRNR") == e["key"].get("ADDRNUMBER")]
            elif e["table"] in owner:
                cls = owner[e["table"]]
                targets = [(cls, CLASSES[cls][0], e["key"])]
            for cls, header, key in targets:
                oid = "".join(self._fmt(d, key.get(d["FIELDNAME"])) for d in self._dfies_raw(header) if d["KEYFLAG"] == "X")
                ts = e["ts"]
                out.append({"MANDANT": self.sap.system.client, "OBJECTCLAS": cls, "OBJECTID": oid, "CHANGENR": f"{e['seq']:010d}", "USERNAME": "SIMUSER",
                            "UDATE": ts[:10], "UTIME": ts[11:19].replace(":", ""), "TCODE": "SIM", "CHANGE_IND": e["op"]})
        return out

    # --- RFC_READ_TABLE
    def _read(self, p: dict) -> dict:
        table = p["QUERY_TABLE"]
        if table in self.denied:
            raise RemoteAuthError("NOT_AUTHORIZED")
        if table not in TABLES and table not in EXTRA_DFIES:
            raise RemoteTableMissing("TABLE_NOT_AVAILABLE")
        dfies = {d["FIELDNAME"]: d for d in self._dfies_raw(table)}
        fields = [f["FIELDNAME"] for f in p.get("FIELDS", [])] or list(dfies)
        for f in fields:
            if f not in dfies:
                raise RemoteError("FIELD_NOT_VALID")
        delim = p.get("DELIMITER", "")
        width = sum(int(dfies[f]["LENG"]) for f in fields) + len(delim) * (len(fields) - 1)
        if width > 512:
            raise RemoteError("DATA_BUFFER_EXCEEDED")
        pred = self._compile(table, p.get("OPTIONS", []))
        order = self._order.setdefault(table, [])
        rows = self._rows(table)
        if len(order) != len(rows):
            order[:] = list(range(len(rows)))
            self._rng.shuffle(order)  # a stable but key-unrelated order
        idx = [i for i in order if pred(rows[i])]
        if table in self.unstable:
            idx = idx[:]
            self._rng.shuffle(idx)  # every call returns a different order: paging cannot work
        skip, cnt = int(p.get("ROWSKIPS", 0) or 0), int(p.get("ROWCOUNT", 0) or 0)
        idx = idx[skip:skip + cnt] if cnt else idx[skip:]
        layout, off = [], 0
        for f in fields:
            layout.append({"FIELDNAME": f, "OFFSET": f"{off:06d}", "LENGTH": dfies[f]["LENG"], "TYPE": "C", "FIELDTEXT": f})
            off += int(dfies[f]["LENG"]) + len(delim)
        data = [{"WA": delim.join(self._fmt(dfies[f], rows[i].get(f)) for f in fields)} for i in idx]
        return {"FIELDS": layout, "DATA": [] if p.get("NO_DATA") else data, "OPTIONS": p.get("OPTIONS", [])}

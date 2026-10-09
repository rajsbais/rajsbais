"""A fake OData V2 endpoint that serves a SimulatedSap through the mapping in odata.py.

It models the documented behaviour of SAP's OData V2 JSON services: `{"d": {"results": [...], "__next": url}}` envelopes with server-driven
paging, dates as /Date(ms)/, decimals as strings, conversion-exit values in external form (no leading zeros), a $filter grammar without IN,
$select, $count, $metadata, HTTP 429 with Retry-After and 401/403/404 responses. It is a MODEL written from documentation; a real
system will differ in details this cannot reveal.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

from ..ddic import TABLES
from .odata import MAPS, EntityMap, Prop
from .rfc import RfcCommunicationError

_TOK = re.compile(r"\s*(datetime'[^']*'|'(?:[^']|'')*'|\(|\)|[A-Za-z_][A-Za-z0-9_]*|-?\d+(?:\.\d+)?)")
HOST = "https://fake-s4.invalid"


class FakeODataTransport:
    def __init__(self, sap, *, page_size: int = 25, denied_services: set[str] | None = None, drop_props: dict[str, set[str]] | None = None,
                 maps: dict[str, EntityMap] | None = None, unstable: bool = False, auth_fail: bool = False):
        self.sap, self.page_size = sap, page_size
        self.maps = maps if maps is not None else MAPS
        self.denied_services, self.drop_props = denied_services or set(), drop_props or {}
        self.unstable, self.auth_fail = unstable, auth_fail
        self.calls: list[str] = []
        self._fail: list[tuple[int, dict]] = []
        self._flip = 0

    def fail_next(self, n: int, status: int = 429, retry_after: str = "1") -> None:
        self._fail += [(status, {"Retry-After": retry_after})] * n

    # ------------------------------------------------------------ routing
    def get(self, url: str) -> tuple[int, dict, str]:
        self.calls.append(url)
        if self._fail:
            st, h = self._fail.pop(0)
            return st, h, ""
        if self.auth_fail:
            return 401, {}, self._err("Authentication failed")
        parsed = urllib.parse.urlparse(url)
        path, q = parsed.path, {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        service, _, rest = path.rpartition("/")
        if rest == "$metadata":
            return self._metadata(path[: -len("/$metadata")])
        if rest == "$count":
            service, _, eset = service.rpartition("/")
            m = self._find(service, eset)
            if isinstance(m, tuple):
                return m
            return 200, {}, str(len(self._rows(m, None)))
        m = self._find(service, rest)
        if isinstance(m, tuple):
            return m
        return self._query(m, q, path)

    def _err(self, msg: str) -> str:
        return json.dumps({"error": {"code": "ERR", "message": {"lang": "en", "value": msg}}})

    def _find(self, service: str, eset: str):
        if service in self.denied_services:
            return 403, {}, self._err("No authorization for service " + service.rsplit("/", 1)[-1])
        for m in self.maps.values():
            if m.service == service and m.entity_set == eset:
                return m
        return 404, {}, self._err("Resource not found")

    # ------------------------------------------------------------ metadata
    def _metadata(self, service: str):
        if service in self.denied_services:
            return 403, {}, self._err("No authorization")
        sets = {m.entity_set: m for m in self.maps.values() if m.service == service}
        if not sets:
            return 404, {}, self._err("Service not found")
        types = "".join(
            f'<EntityType Name="{escape(m.entity_set)}Type">' + "".join(
                f'<Property Name="{escape(p.prop)}" Type="Edm.String"/>' for p in m.props if p.prop not in self.drop_props.get(m.entity_set, set())) + "</EntityType>"
            for m in sets.values())
        es = "".join(f'<EntitySet Name="{escape(n)}" EntityType="SRV.{escape(n)}Type"/>' for n in sets)
        return 200, {"Content-Type": "application/xml"}, (
            '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0"><edmx:DataServices>'
            f'<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="SRV">{types}<EntityContainer Name="C">{es}</EntityContainer></Schema>'
            "</edmx:DataServices></edmx:Edmx>")

    # ------------------------------------------------------------ data
    def _table(self, m: EntityMap) -> str:
        return next(t for t, mm in self.maps.items() if mm is m)

    def _ext(self, p: Prop, v: Any) -> Any:
        """Internal value -> the typed value the server compares on (external form)."""
        if p.kind == "date":
            return str(v)[:10] if v else ""
        if p.kind in ("int", "dec"):
            return float(v or 0)
        s = str(v if v is not None else "")
        return (s.lstrip("0") or "0") if p.alpha and s.isdigit() else s

    def _json(self, p: Prop, v: Any):
        e = self._ext(p, v)
        if p.kind == "date":
            if not e:
                return None
            ms = int(datetime.fromisoformat(e).replace(tzinfo=timezone.utc).timestamp() * 1000)
            return f"/Date({ms})/"
        if p.kind in ("int", "dec"):
            return f"{e:.3f}"
        return e

    def _rows(self, m: EntityMap, flt: str | None) -> list[dict]:
        t = self._table(m)
        td = TABLES[t]
        pred = self._compile(m, flt) if flt else (lambda r: True)
        by = m.by_ddic()
        rows = [r for r in self.sap.data.get(t, []) if pred({p.prop: self._ext(p, r.get(p.ddic)) for p in m.props})]
        rows.sort(key=lambda r: tuple(str(r.get(k)) for k in td.keys))
        if self.unstable:
            self._flip += 1
            rows = rows[self._flip % max(1, len(rows)):] + rows[: self._flip % max(1, len(rows))]
        return rows

    def _query(self, m: EntityMap, q: dict, path: str):
        sel = q.get("$select")
        props = {p.prop: p for p in m.props}
        want = [s for s in sel.split(",")] if sel else list(props)
        for w in want:
            if w not in props or w in self.drop_props.get(m.entity_set, set()):
                return 400, {}, self._err(f"Property '{w}' not found in type '{m.entity_set}Type'")
        try:
            rows = self._rows(m, q.get("$filter"))
        except ValueError as e:
            return 400, {}, self._err(str(e))
        skip = int(q.get("$skiptoken", 0) or 0)
        page = rows[skip: skip + self.page_size]
        d: dict[str, Any] = {"results": [{"__metadata": {"type": f"SRV.{m.entity_set}Type"}, **{w: self._json(props[w], r.get(props[w].ddic)) for w in want}} for r in page]}
        if skip + self.page_size < len(rows):
            nq = dict(q)
            nq["$skiptoken"] = str(skip + self.page_size)
            d["__next"] = HOST + path + "?" + urllib.parse.urlencode(nq, quote_via=urllib.parse.quote)
        return 200, {"Content-Type": "application/json"}, json.dumps({"d": d})

    # ------------------------------------------------------------ $filter
    def _compile(self, m: EntityMap, text: str):
        toks, pos = [], 0
        while pos < len(text):
            mt = _TOK.match(text, pos)
            if not mt:
                if not text[pos:].strip():
                    break
                raise ValueError(f"Invalid filter near: {text[pos:pos + 20]}")
            toks.append(mt.group(1))
            pos = mt.end()
        props = {p.prop: p for p in m.props}
        i = 0

        def peek():
            return toks[i].lower() if i < len(toks) else None

        def take():
            nonlocal i
            if i >= len(toks):
                raise ValueError("Invalid filter: unexpected end")
            i += 1
            return toks[i - 1]

        def literal(p: Prop):
            t = take()
            if t.startswith("datetime'"):
                return t[9:-1][:10]
            if t.startswith("'"):
                return t[1:-1].replace("''", "'")
            return float(t)

        def cond():
            if peek() == "(":
                take()
                e = orexp()
                if take() != ")":
                    raise ValueError("Invalid filter: missing )")
                return e
            name = take()
            if name not in props or name in self.drop_props.get(m.entity_set, set()):
                raise ValueError(f"Property '{name}' not found")
            op = take().lower()
            if op not in ("eq", "ne", "ge", "le", "gt", "lt"):
                raise ValueError(f"Invalid filter operator '{op}'")
            p = props[name]
            val = literal(p)
            if p.kind in ("int", "dec") and not isinstance(val, float):
                raise ValueError("Invalid filter: numeric literal expected")
            if p.kind not in ("int", "dec") and isinstance(val, float):
                raise ValueError("Invalid filter: string literal expected")
            cmp = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "ge": lambda a, b: a >= b, "le": lambda a, b: a <= b,
                   "gt": lambda a, b: a > b, "lt": lambda a, b: a < b}[op]
            return lambda r, n=name, v=val, c=cmp: c(r[n], v)

        def andexp():
            e = cond()
            while peek() == "and":
                take()
                e = (lambda a, b: lambda r: a(r) and b(r))(e, cond())
            return e

        def orexp():
            e = andexp()
            while peek() == "or":
                take()
                e = (lambda a, b: lambda r: a(r) or b(r))(e, andexp())
            return e

        e = orexp()
        if i != len(toks):
            raise ValueError("Invalid filter: trailing tokens")
        return e

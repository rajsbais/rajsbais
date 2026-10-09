"""Read-only SAP adapter over OData V2 (released SAP APIs / CDS-based services, e.g. API_PURCHASEORDER_PROCESS_SRV).

IMPORTANT STATUS: exercised only against `FakeODataTransport` (fake_odata.py), which models the documented OData V2 JSON behaviour. It has
NEVER been run against a real SAP system, the entity and property names below are written from memory of the SAP Business Accelerator Hub
and MUST be verified against the real $metadata, and the real HTTP transport (`HttpODataTransport`) is untested.

Why this is harder than RFC
  The platform thinks in tables and fields (VBAK-ERDAT); released APIs expose business entities with other names, other field sets and
  external formats. So there is an explicit MAPPING per table (entity set, property per field, type, conversion exit). Three consequences:
    * a table may be only PARTIALLY available (a field the API does not expose, e.g. VBAK-BUKRS_VF). The adapter never invents the value:
      the field is None, `gaps()` lists it, filtering on it is refused, and the Planner blocks any plan that touches the table;
    * conversion exits are applied: the API returns '4500000001' / '100001' (no leading zeros on materials), the platform stores the
      internal form ('000000000000100001'); the mapping converts both ways, including inside $filter literals;
    * the API has no IN operator and a limited filter syntax: IN is expanded to OR-chains in batches.
  Paging follows the server's own __next links; every scan is bounded; 429/5xx/communication errors are retried with backoff.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from ..adapter import ChangeLogGap, Row, SapSystem
from ..ddic import TABLES
from .profile import ConnectionProfile, resolve_secret
from .rfc import (RemoteAuthError, RemoteError, RemoteTableMissing, RfcCommunicationError, ScanTooLarge, Stats, UnstablePaging)


class ODataThrottled(RfcCommunicationError):
    """HTTP 429 / 503: retried after the server's Retry-After."""

    def __init__(self, msg: str, retry_after: float = 1.0):
        super().__init__(msg)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Prop:
    ddic: str
    prop: str
    kind: str = "str"  # str | date | int | dec
    alpha: int = 0  # width of the ALPHA / MATN1 conversion exit (internal form is zero-padded to this width), 0 = none


@dataclass(frozen=True)
class EntityMap:
    service: str
    entity_set: str
    props: tuple[Prop, ...]

    def by_ddic(self) -> dict[str, Prop]:
        return {p.ddic: p for p in self.props}

    def by_prop(self) -> dict[str, Prop]:
        return {p.prop: p for p in self.props}


def P(ddic, prop, kind="str", alpha=0):
    return Prop(ddic, prop, kind, alpha)


_PO, _BILL, _PROD = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV", "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV", "/sap/opu/odata/sap/API_PRODUCT_SRV"
# From memory of the SAP Business Accelerator Hub: VERIFY every name against the real $metadata of the target system.
MAPS: dict[str, EntityMap] = {
    "EKKO": EntityMap(_PO, "A_PurchaseOrder", (P("EBELN", "PurchaseOrder", alpha=10), P("BUKRS", "CompanyCode"), P("LIFNR", "Supplier", alpha=10),
                                             P("BEDAT", "PurchaseOrderDate", "date"), P("EKORG", "PurchasingOrganization"), P("BSART", "PurchaseOrderType"))),
    "EKPO": EntityMap(_PO, "A_PurchaseOrderItem", (P("EBELN", "PurchaseOrder", alpha=10), P("EBELP", "PurchaseOrderItem"), P("MATNR", "Material", alpha=18),
                                                 P("WERKS", "Plant"), P("MENGE", "OrderQuantity", "int"), P("NETPR", "NetPriceAmount", "dec"))),
    "VBRK": EntityMap(_BILL, "A_BillingDocument", (P("VBELN", "BillingDocument", alpha=10), P("FKDAT", "BillingDocumentDate", "date"), P("BUKRS", "CompanyCode"),
                                                 P("KUNRG", "PayerParty", alpha=10), P("NETWR", "TotalNetAmount", "dec"), P("WAERK", "TransactionCurrency"))),
    "VBRP": EntityMap(_BILL, "A_BillingDocumentItem", (P("VBELN", "BillingDocument", alpha=10), P("POSNR", "BillingDocumentItem"), P("MATNR", "Material", alpha=18),
                                                     P("FKIMG", "BillingQuantity", "int"), P("NETWR", "NetAmount", "dec"), P("VGBEL", "ReferenceSDDocument", alpha=10),
                                                     P("AUBEL", "SalesDocument", alpha=10), P("AUPOS", "SalesDocumentItem"))),
    "MARA": EntityMap(_PROD, "A_Product", (P("MATNR", "Product", alpha=18), P("MTART", "ProductType"), P("MATKL", "ProductGroup"), P("MEINS", "BaseUnit"),
                                         P("ERSDA", "CreationDate", "date"))),
    "MAKT": EntityMap(_PROD, "A_ProductDescription", (P("MATNR", "Product", alpha=18), P("SPRAS", "Language"), P("MAKTX", "ProductDescription"))),
    "MARC": EntityMap(_PROD, "A_ProductPlant", (P("MATNR", "Product", alpha=18), P("WERKS", "Plant"), P("DISMM", "MRPType"))),
    "T001": EntityMap("/sap/opu/odata/sap/API_COMPANYCODE_SRV", "A_CompanyCode", (P("BUKRS", "CompanyCode"), P("BUTXT", "CompanyCodeName"), P("ORT01", "CityName"), P("WAERS", "Currency"))),
    # partial on purpose: the API has no company code on the plant (T001W-BUKRS is a valuation-area assignment)
    "T001W": EntityMap("/sap/opu/odata/sap/API_PLANT_SRV", "A_Plant", (P("WERKS", "Plant"), P("NAME1", "PlantName"), P("LAND1", "Country"))),
    # partial on purpose: the sales-order header has no company code (it follows from the sales organisation)
    "VBAK": EntityMap("/sap/opu/odata/sap/API_SALES_ORDER_SRV", "A_SalesOrder", (P("VBELN", "SalesOrder", alpha=10), P("ERDAT", "CreationDate", "date"), P("AUART", "SalesOrderType"),
                                                                              P("VKORG", "SalesOrganization"), P("KUNNR", "SoldToParty", alpha=10), P("NETWR", "TotalNetAmount", "dec"),
                                                                              P("WAERK", "TransactionCurrency"), P("ERNAM", "CreatedByUser"))),
}
SERVER_PAGE_HINT = 1000
_DATE_JSON = re.compile(r"^/Date\((-?\d+)(?:[+-]\d+)?\)/$")


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


class ODataTransport(Protocol):
    def get(self, url: str) -> tuple[int, dict, str]:
        """GET a path + query string relative to the system's base URL. Returns (status, headers, body)."""


class HttpODataTransport:
    """Real transport over HTTPS with basic authentication or OAuth 2.0 client credentials. UNTESTED against a real system."""

    def __init__(self, profile: ConnectionProfile):
        from .profile import ProfileError
        if not (profile.user and profile.password_ref):
            raise ProfileError("an odata connection needs user and password_ref (env:NAME or file:/path); anonymous access is not supported")
        self.base = profile.base_url.rstrip("/")
        self.timeout = profile.timeout_s
        self._pw = resolve_secret(profile.password_ref) if profile.password_ref else ""
        self._user, self._token_url = profile.user, profile.options.get("token_url", "")
        self._token: tuple[str, float] | None = None

    def _auth(self) -> str:
        if not self._token_url:
            import base64
            return "Basic " + base64.b64encode(f"{self._user}:{self._pw}".encode()).decode()
        if not self._token or self._token[1] < time.time() + 30:
            body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
            import base64
            req = urllib.request.Request(self._token_url, data=body, headers={"Authorization": "Basic " + base64.b64encode(f"{self._user}:{self._pw}".encode()).decode()})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - https URL from an operator-approved profile
                    d = json.loads(r.read())
            except (urllib.error.URLError, TimeoutError) as e:
                raise RfcCommunicationError(f"token endpoint unreachable: {e}")
            self._token = (d["access_token"], time.time() + int(d.get("expires_in", 300)))
        return "Bearer " + self._token[0]

    def get(self, url: str) -> tuple[int, dict, str]:
        req = urllib.request.Request(self.base + url, headers={"Authorization": self._auth(), "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return r.status, dict(r.headers), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            raise RfcCommunicationError(str(e))


class ODataSourceAdapter:
    """SourceAdapter over OData V2. Only GET is ever issued; there is nothing to write with."""

    kind = "odata"

    def __init__(self, system: SapSystem, transport: ODataTransport, profile: ConnectionProfile | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 reference: Callable[[], date] = date.today, maps: dict[str, EntityMap] | None = None):
        self.system = system
        self.profile = profile or ConnectionProfile(system.sid, "odata", base_url="https://localhost", client=system.client)
        self._t, self._sleep, self._clock, self._reference = transport, sleep, clock, reference
        self.maps = maps if maps is not None else MAPS
        self.stats = Stats()
        self._last_call = 0.0
        self.drift: dict[str, list[str]] = {}
        self._remote_props: dict[str, dict[str, set[str]]] = {}  # service -> entity set -> property names (from $metadata)

    # ------------------------------------------------------------ transport with throttle and retry
    def _call(self, url: str) -> tuple[int, dict, str]:
        gap = 60.0 / max(1, self.profile.calls_per_minute)
        wait = gap - (self._clock() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        last: Exception | None = None
        for attempt in range(4):
            self._last_call = self._clock()
            self.stats.calls += 1
            try:
                status, headers, body = self._t.get(url)
                if status in (429, 503):
                    ra = float({k.lower(): v for k, v in headers.items()}.get("retry-after", 1) or 1)
                    raise ODataThrottled(f"HTTP {status}", min(ra, 60.0))
                if status >= 500:
                    raise RfcCommunicationError(f"HTTP {status}")
            except ODataThrottled as e:
                last = e
                self.stats.retries += 1
                self._sleep(e.retry_after)
                continue
            except RfcCommunicationError as e:
                last = e
                self.stats.retries += 1
                self._sleep(0.5 * 2 ** attempt)
                continue
            if status in (401, 403):
                raise RemoteAuthError(f"HTTP {status}: {self._message(body)}")
            if status == 404:
                raise RemoteTableMissing(f"HTTP 404 for {url.split('?')[0]}")
            if status >= 400:
                raise RemoteError(f"HTTP {status}: {self._message(body)}")
            return status, headers, body
        raise RfcCommunicationError(f"GET {url.split('?')[0]} failed after 4 attempts: {last}")

    @staticmethod
    def _message(body: str) -> str:
        try:
            return str(json.loads(body)["error"]["message"]["value"])[:200]
        except Exception:  # noqa: BLE001
            return body[:120]

    def ping(self) -> None:
        svc = next(iter(self.maps.values())).service
        self._call(f"{svc}/$metadata")

    # ------------------------------------------------------------ mapping, conversion and gaps
    def _map(self, table: str) -> EntityMap:
        m = self.maps.get(table)
        if m is None:
            raise RemoteTableMissing(f"table {table} has no OData mapping: it cannot be read through this connection")
        return m

    def mapped(self, table: str) -> bool:
        return table in self.maps

    def gaps(self) -> dict[str, list[str]]:
        """table -> fields of the platform's DDIC model that the API does not supply ('*' = the whole table is unavailable)."""
        out: dict[str, list[str]] = {}
        for t, td in TABLES.items():
            if td.config and t not in ("T001", "T001W"):
                continue
            m = self.maps.get(t)
            miss = list(td.fields) if m is None else [f for f in td.fields if f not in m.by_ddic()]
            if miss:
                out[t] = ["*"] if m is None else miss
        return out

    def _to_internal(self, p: Prop, v: Any) -> Any:
        if v is None:
            return None if p.kind != "str" else ""
        if p.kind == "date":
            if isinstance(v, str):
                m = _DATE_JSON.match(v)
                if m:
                    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(m.group(1)))).date().isoformat()
                if re.match(r"^\d{4}-\d{2}-\d{2}", v):
                    return v[:10]
            return ""
        if p.kind == "int":
            return int(float(v)) if v not in ("", None) else 0
        if p.kind == "dec":
            return float(v) if v not in ("", None) else 0.0
        s = str(v)
        return s.zfill(p.alpha) if p.alpha and s.isdigit() else s

    def _to_external(self, p: Prop, v: Any) -> str:
        """A platform value as a $filter literal."""
        if p.kind == "date":
            return f"datetime'{str(v)[:10]}T00:00:00'"
        if p.kind in ("int", "dec"):
            return str(v)
        s = str(v)
        if p.alpha and s.isdigit():
            s = s.lstrip("0") or "0"
        return _q(s)

    def _convert(self, table: str, m: EntityMap, d: dict) -> Row:
        by = m.by_ddic()
        return {f: (self._to_internal(by[f], d.get(by[f].prop)) if f in by else None) for f in TABLES[table].fields}

    # ------------------------------------------------------------ $filter
    def _cond(self, table: str, field_: str, op: str, value: Any) -> str:
        m = self._map(table)
        p = m.by_ddic().get(field_)
        if p is None:
            raise RemoteError(f"{table}-{field_} is not exposed by the OData API ({m.entity_set}): it cannot be used to select rows")
        return f"{p.prop} {op} {self._to_external(p, value)}"

    # ------------------------------------------------------------ reading
    def _url(self, m: EntityMap, flt: str | None, extra: dict | None = None) -> str:
        params = {"$format": "json", **({"$filter": flt} if flt else {}), **(extra or {})}
        return f"{m.service}/{m.entity_set}?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

    def _fetch(self, table: str, flt: str | None) -> list[Row]:
        m = self._map(table)
        keys = TABLES[table].keys
        url, out, seen = self._url(m, flt, {"$select": ",".join(p.prop for p in m.props)}), [], set()
        while url:
            _s, _h, body = self._call(url)
            try:
                d = json.loads(body)["d"]
            except (ValueError, KeyError):
                raise RemoteError(f"{table}: the response is not an OData V2 JSON document")
            for r in d.get("results", []):
                row = self._convert(table, m, r)
                k = tuple(row[f] for f in keys)
                if k in seen:
                    raise UnstablePaging(f"{table}: key {k} returned twice while following the server's paging links: the source is changing during the read")
                seen.add(k)
                out.append(row)
            if len(out) > self.profile.max_scan_rows:
                self.stats.guard_trips += 1
                raise ScanTooLarge(f"{table}: more than {self.profile.max_scan_rows} rows match; narrow the selection")
            nxt = d.get("__next")
            if nxt:
                parsed = urllib.parse.urlparse(nxt)
                url = parsed.path + ("?" + parsed.query if parsed.query else "") if parsed.path.startswith("/") else f"{m.service}/{nxt}"
            else:
                url = ""
        self.stats.rows += len(out)
        self.stats.tables[table] = self.stats.tables.get(table, 0) + len(out)
        return out

    # ------------------------------------------------------------ SourceAdapter (read side)
    def reference_date(self) -> date:
        return self._reference()

    def get(self, table: str, key: tuple) -> Row | None:
        flt = " and ".join(self._cond(table, f, "eq", v) for f, v in zip(TABLES[table].keys, key))
        rows = self._fetch(table, flt)
        return rows[0] if rows else None

    def lookup(self, table: str, field_: str, value) -> list[Row]:
        return self._fetch(table, self._cond(table, field_, "eq", value))

    def select_in(self, table: str, field_: str, values) -> list[Row]:
        """No IN operator in OData V2: OR-chains in batches of 40."""
        vals, out, seen = sorted({str(v) for v in values}), [], set()
        keys = TABLES[table].keys
        for i in range(0, len(vals), 40):
            flt = "(" + " or ".join(self._cond(table, field_, "eq", v) for v in vals[i:i + 40]) + ")"
            for r in self._fetch(table, flt):
                k = tuple(r[f] for f in keys)
                if k not in seen:
                    seen.add(k)
                    out.append(r)
        return out

    def select_between(self, table: str, field_: str, lo: str, hi: str) -> list[Row]:
        return self._fetch(table, f"{self._cond(table, field_, 'ge', lo)} and {self._cond(table, field_, 'le', hi)}")

    def select(self, table: str, predicate: Callable[[Row], bool] | None = None) -> list[Row]:
        self.stats.scans += 1
        self.stats.scanned.append(table)
        return [r for r in self._fetch(table, None) if predicate is None or predicate(r)]

    def count(self, table: str) -> int:
        self.stats.scans += 1
        m = self._map(table)
        _s, _h, body = self._call(f"{m.service}/{m.entity_set}/$count")
        try:
            return int(body.strip())
        except ValueError:
            raise RemoteError(f"{table}: $count did not return a number")

    def table_counts(self) -> dict[str, int]:
        out = {}
        for t in TABLES:
            if t not in self.maps:
                continue
            try:
                n = self.count(t)
            except RemoteTableMissing:
                continue
            if n:
                out[t] = n
        return out

    def discover(self) -> dict:
        cc = [{"code": r["BUKRS"], "name": r["BUTXT"], "currency": r["WAERS"]} for r in self.select("T001")] if self.mapped("T001") else []
        pl = [{"plant": r["WERKS"], "name": r["NAME1"], "company_code": r["BUKRS"]} for r in self.select("T001W")] if self.mapped("T001W") else []
        return {"simulated": False, "system": self.system.model_dump(mode="json"), "company_codes": cc, "plants": pl, "table_counts": {}, "custom_fields": [],
                "installed_components": [], "remote": {"kind": "odata", "host": self.profile.base_url, "database": "", "stats": self.stats.public(),
                                                       "gaps": self.gaps()}}

    # ------------------------------------------------------------ metadata
    def schema_drift(self, tables=None) -> dict[str, list[str]]:
        """Compare the mapping with the system's $metadata: a mapped property that does not exist remotely is drift."""
        for t, m in self.maps.items():
            if tables and t not in tables:
                continue
            try:
                props = self._service_props(m.service).get(m.entity_set)
            except RemoteTableMissing:
                self.drift[t] = [f"service {m.service} is not available"]
                continue
            if props is None:
                self.drift[t] = [f"entity set {m.entity_set} is not in the service"]
                continue
            miss = [p.prop for p in m.props if p.prop not in props]
            if miss:
                self.drift[t] = [f"property {x} missing in {m.entity_set}" for x in miss]
        return dict(self.drift)

    def _service_props(self, service: str) -> dict[str, set[str]]:
        if service not in self._remote_props:
            _s, _h, xml = self._call(f"{service}/$metadata")
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                raise RemoteError(f"{service}: $metadata is not valid XML")
            types: dict[str, set[str]] = {}
            sets: dict[str, str] = {}
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag == "EntityType":
                    types[el.get("Name", "")] = {p.get("Name", "") for p in el if p.tag.split("}")[-1] == "Property"}
                elif tag == "EntitySet":
                    sets[el.get("Name", "")] = (el.get("EntityType") or "").split(".")[-1]
            self._remote_props[service] = {s: types.get(t, set()) for s, t in sets.items()}
        return self._remote_props[service]

    # ------------------------------------------------------------ change documents: not available through released APIs
    def change_seq(self) -> int:
        return 0

    def changes_since(self, seq: int) -> list[dict]:
        raise ChangeLogGap("released OData APIs expose no change documents; a full compare is needed")

    def change_coverage(self):
        return None

    def capabilities(self) -> dict:
        g = self.gaps()
        return {"pushdown": ["lookup", "get", "select_in (OR chains)", "select_between"], "full_scan": f"bounded to {self.profile.max_scan_rows} rows",
                "change_documents": False, "writes": False, "validated_against_real_sap": False,
                "mapped_tables": sorted(self.maps), "tables_with_gaps": {t: v for t, v in g.items() if v != ["*"]},
                "tables_unavailable": sorted(t for t, v in g.items() if v == ["*"])}

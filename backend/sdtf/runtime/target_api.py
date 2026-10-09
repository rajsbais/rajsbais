"""Target S/4HANA API access: transport contract, simulated gateway, HTTP transport, typed client (ADR-0015).

`ApiTransport.request(method, service, path, payload, headers)` is the only thing loaders depend on.
* `SimulatedS4Gateway` implements the released services the bindings use over the target's record store: CSRF
  protection, ETags with If-Match, OData-style keys and errors, deep inserts, validation against configuration
  (company codes, sales organisations, plants), derived fields (pricing, statuses), target-side numbering for
  journal entries, reversal postings, block flags for masters. It writes `sap_records`, so everything downstream
  (reconciliation, delta checks) sees what "the API" accepted. It is the executable contract for a real target.
* `S4ApiHttpTransport` talks to a real system through httpx: CSRF token fetch, basic or OAuth2 client-credentials
  auth, OData V2 JSON, a SOAP envelope for the journal entry service. Not verified against a live system here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..catalog.api_bindings import API_BINDINGS, EXT_PREFIX, ApiBinding, EntityBinding
from ..catalog.store import RecordStore, delete_records, upsert_records
from ..catalog.tables import record_key


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class ApiUnavailable(ApiError):
    def __init__(self, message: str):
        super().__init__(503, "API_UNAVAILABLE", message)


@dataclass
class ApiResponse:
    status: int
    body: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)


def _etag(row: dict) -> str:
    return 'W/"' + hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()[:16] + '"'


_KEY_RE = re.compile(r"^(?P<set>[A-Za-z0-9_]+)\((?P<key>.*)\)$")


def parse_path(path: str) -> tuple[str, dict | None]:
    """'A_SalesOrder' -> ('A_SalesOrder', None); "A_SalesOrderItem(SalesOrder='1',SalesOrderItem='10')" -> (set, {..})"""
    m = _KEY_RE.match(path.strip())
    if not m:
        return path.strip(), None
    raw = m.group("key")
    key: dict = {}
    if "=" not in raw:
        key["__single__"] = raw.strip("'")
    else:
        for part in re.findall(r"([A-Za-z0-9_]+)='((?:[^']|'')*)'", raw):
            key[part[0]] = part[1].replace("''", "'")
    return m.group("set"), key


def format_key(props: tuple[str, ...], values: list[str]) -> str:
    if len(props) == 1:
        return f"('{values[0]}')"
    return "(" + ",".join(f"{p}='{v}'" for p, v in zip(props, values, strict=False)) + ")"


# ------------------------------------------------------------------------------------------ simulated gateway
_SERVICE_ENTITIES: dict[str, dict[str, tuple[ApiBinding, EntityBinding]]] = {}
for _b in API_BINDINGS.values():
    _SERVICE_ENTITIES.setdefault(_b.service, {})[_b.header.entity_set] = (_b, _b.header)
    for _it in _b.items.values():
        _SERVICE_ENTITIES.setdefault(_b.service, {})[_it.entity_set] = (_b, _it)


class SimulatedS4Gateway:
    """The released APIs of a prepared S/4HANA target, simulated over its record store (no SAP system)."""

    name = "SIMULATED_S4"

    def __init__(self, session, target_system_id: str, numbering: dict[str, str] | None = None):
        self.session = session
        self.sid = target_system_id
        self.numbering = {"JournalEntry": "internal", **(numbering or {})}
        self._tokens: set[str] = set()
        self._store = RecordStore(target_system_id)
        self._loaded: set[str] = set()
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, str, int]] = []

    # -- storage
    def _ensure(self, *tables: str) -> None:
        missing = [t for t in tables if t not in self._loaded]
        if missing:
            fresh = RecordStore.load(self.session, self.sid, tables=missing)
            for t in missing:
                self._store._tables[t] = list(fresh._tables.get(t, []))
                self._store._by_key[t] = dict(fresh._by_key.get(t, {}))
                self._loaded.add(t)
                self._store._indexes = {k: v for k, v in self._store._indexes.items() if k[0] != t}

    def row(self, table: str, key: str) -> dict | None:
        self._ensure(table)
        return self._store.by_key(table, key)

    def rows(self, table: str) -> list[dict]:
        self._ensure(table)
        return self._store.rows(table)

    def _write(self, table: str, row: dict) -> None:
        self._ensure(table)
        key = record_key(table, row)
        upsert_records(self.session, self.sid, table, [row])
        old = self._store._by_key[table].get(key)
        if old is not None:
            self._store._tables[table] = [r for r in self._store._tables[table] if r is not old]
        self._store._tables[table].append(row)
        self._store._by_key[table][key] = row
        self._store._indexes = {k: v for k, v in self._store._indexes.items() if k[0] != table}

    def _delete(self, table: str, key: str) -> None:
        self._ensure(table)
        delete_records(self.session, self.sid, table, [key])
        old = self._store._by_key[table].pop(key, None)
        if old is not None:
            self._store._tables[table] = [r for r in self._store._tables[table] if r is not old]
        self._store._indexes = {k: v for k, v in self._store._indexes.items() if k[0] != table}

    # -- helpers
    @staticmethod
    def _err(status: int, code: str, message: str) -> ApiResponse:
        return ApiResponse(status, {"error": {"code": code, "message": {"lang": "en", "value": message}}})

    def _check_csrf(self, method: str, headers: dict) -> ApiResponse | None:
        if method in ("POST", "PATCH", "PUT", "DELETE") and headers.get("x-csrf-token") not in self._tokens:
            return self._err(403, "CSRF", "CSRF token validation failed")
        return None

    def _key_values(self, eb: EntityBinding, key: dict | None, entity: dict | None) -> list[str]:
        if key and "__single__" in key:
            return [key["__single__"]]
        src = key or entity or {}
        return ["" if src.get(p) is None else str(src.get(p)) for p in eb.key_props]

    def _validate(self, b: ApiBinding, eb: EntityBinding, row: dict) -> str | None:
        """Configuration checks a real target performs before accepting a document."""
        if eb.table in ("VBAK",):
            self._ensure("TVKO")
            org = self._store.get("TVKO", VKORG=row.get("VKORG"))
            if org is None:
                return f"sales organization {row.get('VKORG')} is not defined in the target"
            row["BUKRS_VF"] = org["BUKRS"]
        if eb.table in ("EKKO", "BKPF", "KNB1", "LFB1", "CSKS", "CEPC") and row.get("BUKRS"):
            self._ensure("T001")
            if self._store.by_key("T001", str(row["BUKRS"])) is None:
                return f"company code {row['BUKRS']} is not defined in the target"
        if eb.table in ("VBAP", "EKPO", "LIPS", "MARC") and row.get("WERKS"):
            self._ensure("T001W")
            if self._store.by_key("T001W", str(row["WERKS"])) is None:
                return f"plant {row['WERKS']} is not defined in the target"
        if eb.table == "LIKP":
            self._ensure("T001K")
            k = self._store.get("T001K", BWKEY=row.get("WERKS"))
            if k is None:
                return f"plant {row.get('WERKS')} has no valuation area / company code in the target"
            row["BUKRS"] = k["BUKRS"]
        return None

    def _derive(self, b: ApiBinding, eb: EntityBinding, row: dict, previous: dict | None) -> None:
        """Pricing and status derivation the target owns."""
        if eb.table in ("VBAP", "EKPO"):
            qty_f, amt_f = ("KWMENG", "NETWR") if eb.table == "VBAP" else ("MENGE", "NETWR")
            if previous is not None and previous.get(qty_f) not in (None, 0, "0") and (row.get(amt_f) is None or row.get(qty_f) != previous.get(qty_f)):
                unit = float(previous[amt_f]) / float(previous[qty_f])
                row[amt_f] = round(unit * float(row[qty_f]), 2)
            elif eb.table == "EKPO" and row.get("NETPR") is not None and row.get("MENGE") is not None and row.get(amt_f) is None:
                row[amt_f] = round(float(row["NETPR"]) * float(row["MENGE"]), 2)
        if eb.table == "VBAK":
            row.setdefault("GBSTK", "A")
        if eb.table == "BKPF" and row.get("BUDAT"):
            row["MONAT"] = int(str(row["BUDAT"])[4:6] or 0)

    def _recompute_header(self, b: ApiBinding, header_table: str, header_key: str) -> None:
        if header_table not in ("VBAK",):
            return
        hdr = self.row("VBAK", header_key)
        if hdr is None:
            return
        self._ensure("VBAP")
        total = round(sum(float(r.get("NETWR") or 0) for r in self._store.lookup("VBAP", "VBELN", header_key)), 2)
        if hdr.get("NETWR") != total:
            self._write("VBAK", {**hdr, "NETWR": total})

    # -- entry point
    def request(self, method: str, service: str, path: str, payload: dict | None = None, headers: dict | None = None) -> ApiResponse:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        method = method.upper()
        with self._lock:
            resp = self._dispatch(method, service, path, payload, headers)
            self.calls.append((service, method, path.split("(")[0], resp.status))
            return resp

    def _dispatch(self, method: str, service: str, path: str, payload: dict | None, headers: dict) -> ApiResponse:
        if service not in _SERVICE_ENTITIES:
            return self._err(404, "SERVICE_NOT_FOUND", f"service {service} is not activated on this target")
        if method == "GET" and headers.get("x-csrf-token") == "fetch":
            tok = secrets.token_hex(8)
            self._tokens.add(tok)
            return ApiResponse(200, {"d": {"EntitySets": sorted(_SERVICE_ENTITIES[service])}}, {"x-csrf-token": tok})
        if service == "API_JOURNALENTRY_SRV":  # SOAP: authenticated transport, no CSRF handshake
            return self._journal(method, path, payload or {})
        if (e := self._check_csrf(method, headers)) is not None:
            return e
        entity_set, key = parse_path(path)
        if entity_set not in _SERVICE_ENTITIES[service]:
            return self._err(404, "ENTITY_NOT_FOUND", f"entity set {entity_set} does not exist in {service}")
        b, eb = _SERVICE_ENTITIES[service][entity_set]
        if method == "GET":
            if key is None:
                return self._err(400, "UNSUPPORTED", "collection queries are not needed by the loaders; read by key")
            row = self.row(eb.table, "|".join(self._key_values(eb, key, None)))
            if row is None:
                return self._err(404, "NOT_FOUND", f"{entity_set}{path[len(entity_set):]} not found")
            return ApiResponse(200, {"d": eb.to_entity(row) | {p: row.get(f) for f, p in eb.fields.items() if f in eb.derived}}, {"etag": _etag(row)})
        if method == "POST":
            return self._create(b, eb, payload or {})
        if method == "PATCH":
            return self._update(b, eb, key, payload or {}, headers)
        if method == "DELETE":
            return self._remove(b, eb, key, headers)
        return self._err(405, "METHOD_NOT_ALLOWED", method)

    def _create(self, b: ApiBinding, eb: EntityBinding, entity: dict) -> ApiResponse:
        row = eb.to_row(entity)
        if eb is b.header and b.numbering == "internal" and b.service != "API_JOURNALENTRY_SRV":
            row[list(eb.fields)[0]] = self._next_number(eb.table)
        key = record_key(eb.table, row)
        if self.row(eb.table, key) is not None:
            return self._err(409, "DUPLICATE", f"{eb.entity_set} {key} already exists")
        if eb is not b.header and eb.parent_props:
            parent_key = "|".join(str(entity.get(p, "")) for p in eb.parent_props)
            if self.row(b.header.table, parent_key) is None:
                return self._err(404, "PARENT_NOT_FOUND", f"{b.header.entity_set} {parent_key} does not exist")
        err = self._validate(b, eb, row)
        if err:
            return self._err(400, "VALIDATION", err)
        self._derive(b, eb, row, None)
        created_items: list[dict] = []
        if eb is b.header:
            for nav, items in ((k, v) for k, v in entity.items() if k.startswith("to_") and isinstance(v, list)):
                item_eb = next((ib for ib in b.items.values() if nav.lower().endswith(ib.entity_set.lower().replace("a_", "").replace(b.header.entity_set.lower().replace("a_", ""), "")) or nav == f"to_{ib.entity_set[2:]}"), None) or (next(iter(b.items.values())) if b.items else None)
                if item_eb is None:
                    return self._err(400, "VALIDATION", f"navigation {nav} unknown")
                for it in items:
                    irow = item_eb.to_row(it)
                    for f, p in item_eb.fields.items():
                        if p in item_eb.parent_props:
                            irow[f] = row[list(eb.fields)[list(eb.fields.values()).index(p)]] if p in eb.fields.values() else row.get(f)
                    e2 = self._validate(b, item_eb, irow)
                    if e2:
                        return self._err(400, "VALIDATION", e2)
                    self._derive(b, item_eb, irow, None)
                    created_items.append((item_eb, irow))
        self._write(eb.table, row)
        for item_eb, irow in created_items:
            self._write(item_eb.table, irow)
        if eb is not b.header:
            self._recompute_header(b, b.header.table, "|".join(str(row.get(f, "")) for f, p in eb.fields.items() if p in eb.parent_props))
        elif created_items:
            self._recompute_header(b, eb.table, key)
        row = self.row(eb.table, record_key(eb.table, row)) or row
        return ApiResponse(201, {"d": eb.to_entity(row) | {p: row.get(f) for f, p in eb.fields.items() if f in eb.derived}}, {"etag": _etag(row)})

    def _update(self, b: ApiBinding, eb: EntityBinding, key: dict | None, patch: dict, headers: dict) -> ApiResponse:
        if key is None:
            return self._err(400, "KEY_REQUIRED", "PATCH needs an entity key")
        rk = "|".join(self._key_values(eb, key, None))
        row = self.row(eb.table, rk)
        if row is None:
            return self._err(404, "NOT_FOUND", f"{eb.entity_set} {rk} not found")
        if headers.get("if-match") not in (None, "*", _etag(row)):
            return self._err(412, "PRECONDITION_FAILED", "ETag mismatch: the entity changed since it was read")
        changes = eb.to_row(patch)
        for f in changes:
            if f in eb.derived or f in eb.priced:
                return self._err(400, "READ_ONLY", f"{eb.fields.get(f, f)} is derived by the target and cannot be set")
            if f in TABLE_KEYS(eb.table):
                if str(changes[f]) != str(row.get(f)):
                    return self._err(400, "KEY_IMMUTABLE", f"{eb.fields.get(f, f)} is a key")
                continue
            if f not in eb.updatable and not f.startswith(EXT_PREFIX) and f in eb.fields:
                if changes[f] != row.get(f):
                    return self._err(400, "NOT_UPDATABLE", f"{eb.fields.get(f, f)} cannot be changed on an existing {eb.entity_set}")
        new = {**row, **{f: v for f, v in changes.items() if f not in TABLE_KEYS(eb.table)}}
        err = self._validate(b, eb, new)
        if err:
            return self._err(400, "VALIDATION", err)
        self._derive(b, eb, new, row)
        self._write(eb.table, new)
        if eb is not b.header:
            self._recompute_header(b, b.header.table, "|".join(str(new.get(f, "")) for f, p in eb.fields.items() if p in eb.parent_props))
        return ApiResponse(204, {}, {"etag": _etag(new)})

    def _remove(self, b: ApiBinding, eb: EntityBinding, key: dict | None, headers: dict) -> ApiResponse:
        if key is None:
            return self._err(400, "KEY_REQUIRED", "DELETE needs an entity key")
        rk = "|".join(self._key_values(eb, key, None))
        row = self.row(eb.table, rk)
        if row is None:
            return self._err(404, "NOT_FOUND", f"{eb.entity_set} {rk} not found")
        if headers.get("if-match") not in (None, "*", _etag(row)):
            return self._err(412, "PRECONDITION_FAILED", "ETag mismatch")
        if eb is b.header and b.on_delete != "DELETE":
            return self._err(405, "DELETE_NOT_ALLOWED", f"{eb.entity_set} cannot be deleted through the API ({b.on_delete.lower()} instead)")
        if eb is b.header:
            for item_eb in b.items.values():
                self._ensure(item_eb.table)
                parent_fields = [f for f, p in item_eb.fields.items() if p in item_eb.parent_props]
                for r in list(self._store.rows(item_eb.table)):
                    if all(str(r.get(f)) == str(row.get(f)) for f in parent_fields):
                        self._delete(item_eb.table, record_key(item_eb.table, r))
        self._delete(eb.table, rk)
        if eb is not b.header:
            self._recompute_header(b, b.header.table, "|".join(str(row.get(f, "")) for f, p in eb.fields.items() if p in eb.parent_props))
        return ApiResponse(204, {}, {})

    # -- numbering
    def _next_number(self, table: str, company_code: str | None = None, width: int = 10) -> str:
        self._ensure(table)
        nums = [int(r[k]) for r in self._store.rows(table) for k in (list(API_BINDINGS_BY_TABLE[table].fields)[0],) if str(r.get(k, "")).isdigit() and (company_code is None or str(r.get("BUKRS")) == company_code)]
        return str((max(nums) if nums else 0) + 1).zfill(width)

    # -- journal entries (SOAP service, simulated as operations)
    def _journal(self, method: str, operation: str, payload: dict) -> ApiResponse:
        if method != "POST":
            return self._err(405, "METHOD_NOT_ALLOWED", "SOAP operations are POST")
        b = API_BINDINGS["FI.AccountingDocument"]
        if operation == "JournalEntryBulkCreateRequestConfirmation_In":
            return self._post_journal(b, payload)
        if operation == "JournalEntryReverse":
            return self._reverse_journal(b, payload)
        return self._err(404, "OPERATION_NOT_FOUND", operation)

    def _post_journal(self, b: ApiBinding, payload: dict) -> ApiResponse:
        hdr = b.header.to_row({k: v for k, v in payload.items() if k != "Items"})
        items = [b.items["BSEG"].to_row(i) for i in payload.get("Items", [])]
        if not items:
            return self._err(400, "VALIDATION", "a journal entry needs at least one item")
        err = self._validate(b, b.header, hdr)
        if err:
            return self._err(400, "VALIDATION", err)
        bal = round(sum(float(i.get("DMBTR") or 0) * (1 if i.get("SHKZG") == "S" else -1) for i in items), 2)
        if abs(bal) > 0.005:
            return self._err(400, "VALIDATION", f"journal entry does not balance (debits minus credits = {bal})")
        numbering = self.numbering.get("JournalEntry", "internal")
        if numbering == "internal" or not hdr.get("BELNR"):
            self._ensure("BKPF")
            cc_docs = [int(r["BELNR"]) for r in self._store.rows("BKPF") if str(r.get("BUKRS")) == str(hdr.get("BUKRS")) and str(r.get("BELNR", "")).isdigit()]
            hdr["BELNR"] = str((max(cc_docs) if cc_docs else 100000000) + 1)
        key = record_key("BKPF", hdr)
        if self.row("BKPF", key) is not None:
            return self._err(409, "DUPLICATE", f"accounting document {key} already exists")
        self._derive(b, b.header, hdr, None)
        self._write("BKPF", hdr)
        for n, it in enumerate(items, 1):
            it.update({"BUKRS": hdr["BUKRS"], "BELNR": hdr["BELNR"], "GJAHR": hdr["GJAHR"]})
            it.setdefault("BUZEI", n)
            self._write("BSEG", it)
            if it.get("KOART") == "D" and it.get("KUNNR"):
                self._write("BSID", {"BUKRS": hdr["BUKRS"], "KUNNR": it["KUNNR"], "UMSKS": "", "UMSKZ": "", "AUGDT": "", "AUGBL": "", "ZUONR": "", "GJAHR": hdr["GJAHR"], "BELNR": hdr["BELNR"], "BUZEI": it["BUZEI"], "DMBTR": it.get("DMBTR"), "SHKZG": it.get("SHKZG")})
            if it.get("KOART") == "K" and it.get("LIFNR"):
                self._write("BSIK", {"BUKRS": hdr["BUKRS"], "LIFNR": it["LIFNR"], "UMSKS": "", "UMSKZ": "", "AUGDT": "", "AUGBL": "", "ZUONR": "", "GJAHR": hdr["GJAHR"], "BELNR": hdr["BELNR"], "BUZEI": it["BUZEI"], "DMBTR": it.get("DMBTR"), "SHKZG": it.get("SHKZG")})
        return ApiResponse(201, {"d": {"CompanyCode": hdr["BUKRS"], "AccountingDocument": hdr["BELNR"], "FiscalYear": hdr["GJAHR"], "Items": len(items), "numbering": numbering}})

    def _reverse_journal(self, b: ApiBinding, payload: dict) -> ApiResponse:
        key = "|".join(str(payload.get(p, "")) for p in ("CompanyCode", "AccountingDocument", "FiscalYear"))
        hdr = self.row("BKPF", key)
        if hdr is None:
            return self._err(404, "NOT_FOUND", f"accounting document {key} not found")
        self._ensure("BSEG")
        lines = [l for l in self._store.lookup("BSEG", "BELNR", hdr["BELNR"]) if str(l["BUKRS"]) == str(hdr["BUKRS"]) and str(l["GJAHR"]) == str(hdr["GJAHR"])]
        if any(r.get("AWTYP") == "REVERSAL" and r.get("XBLNR") == f"REV {hdr['BELNR']}" for r in self._store.rows("BKPF")):
            return self._err(409, "ALREADY_REVERSED", f"document {key} was already reversed")
        rev = {**hdr, "AWTYP": "REVERSAL", "AWKEY": f"{hdr['BELNR']}{hdr['GJAHR']}", "XBLNR": f"REV {hdr['BELNR']}", "BUDAT": payload.get("PostingDate") or hdr.get("BUDAT"), "BLDAT": payload.get("PostingDate") or hdr.get("BLDAT")}
        resp = self._post_journal(b, {**b.header.to_entity(rev), "Items": [b.items["BSEG"].to_entity({**l, "SHKZG": "H" if l.get("SHKZG") == "S" else "S", "BELNR": None}) for l in lines]})
        if resp.status != 201:
            return resp
        resp.body["d"]["ReversedDocument"] = hdr["BELNR"]
        return resp


def TABLE_KEYS(table: str) -> tuple[str, ...]:
    from ..catalog.tables import TABLES

    td = TABLES.get(table)
    return td.key_fields if td else ()


API_BINDINGS_BY_TABLE: dict[str, EntityBinding] = {}
for _b in API_BINDINGS.values():
    API_BINDINGS_BY_TABLE[_b.header.table] = _b.header
    for _it in _b.items.values():
        API_BINDINGS_BY_TABLE[_it.table] = _it


# --------------------------------------------------------------------------------------------- HTTP transport
SECRET_KEYS = ("passwd", "password", "client_secret")


def resolve_api_destination(sid: str, meta: dict | None = None) -> dict:
    """Base URL and credentials for a target: `meta.api.dest` (secrets as 'env:NAME') or `SDTF_S4_API_<SID>` (JSON)."""
    api = (meta or {}).get("api", {}) if meta else {}
    dest = dict(api.get("dest") or {})
    if not dest:
        raw = os.getenv(f"SDTF_S4_API_{sid.upper()}", "")
        dest = json.loads(raw) if raw else {}
    out = {}
    for k, v in dest.items():
        out[k] = os.getenv(v[4:], "") if isinstance(v, str) and v.startswith("env:") else v
    pw = os.getenv(f"SDTF_S4_API_{sid.upper()}_PASSWD")
    if pw and not out.get("passwd"):
        out["passwd"] = pw
    return out


def mask_api_destination(dest: dict) -> dict:
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in dest.items()}


class S4ApiHttpTransport:
    """Released OData V2 / SOAP services of a real S/4HANA system over HTTPS (httpx). CSRF token fetched once per
    session and refreshed on 403; basic auth or OAuth2 client credentials; SOAP envelope for the journal entry
    service. Unverified against a live system in this repository."""

    name = "HTTP"

    def __init__(self, dest: dict, client=None):
        import httpx

        if not dest.get("base_url"):
            raise ApiUnavailable("API destination needs base_url (e.g. https://my-s4.example.com)")
        self.base = dest["base_url"].rstrip("/")
        self.dest = dest
        self._client = client or httpx.Client(timeout=float(dest.get("timeout", 30)), verify=dest.get("verify", True))
        self._csrf: str | None = None
        self._lock = threading.Lock()
        self._bearer: str | None = None
        self._bearer_exp = 0.0

    def _auth_headers(self) -> dict:
        d = self.dest
        if d.get("token_url") and d.get("client_id"):
            if not self._bearer or time.time() > self._bearer_exp - 30:
                r = self._client.post(d["token_url"], data={"grant_type": "client_credentials", "client_id": d["client_id"], "client_secret": d.get("client_secret", "")})
                if r.status_code != 200:
                    raise ApiError(r.status_code, "OAUTH", f"token endpoint rejected the client credentials: {r.text[:200]}")
                tok = r.json()
                self._bearer, self._bearer_exp = tok["access_token"], time.time() + float(tok.get("expires_in", 3600))
            return {"Authorization": f"Bearer {self._bearer}"}
        if d.get("user"):
            import base64

            return {"Authorization": "Basic " + base64.b64encode(f"{d['user']}:{d.get('passwd', '')}".encode()).decode()}
        return {}

    def _fetch_csrf(self, service: str) -> str:
        r = self._client.get(f"{self.base}/sap/opu/odata/sap/{service}/", headers={**self._auth_headers(), "x-csrf-token": "fetch", "Accept": "application/json"})
        if r.status_code >= 400:
            raise ApiError(r.status_code, "CSRF_FETCH", r.text[:200])
        self._csrf = r.headers.get("x-csrf-token", "")
        return self._csrf

    def request(self, method: str, service: str, path: str, payload: dict | None = None, headers: dict | None = None) -> ApiResponse:
        method = method.upper()
        headers = dict(headers or {})
        with self._lock:
            if service == "API_JOURNALENTRY_SRV":
                return self._soap(path, payload or {})
            if method != "GET" and not self._csrf:
                self._fetch_csrf(service)
            for attempt in (1, 2):
                h = {**self._auth_headers(), "Accept": "application/json", "Content-Type": "application/json", **headers}
                if method != "GET":
                    h["x-csrf-token"] = self._csrf or ""
                url = f"{self.base}/sap/opu/odata/sap/{service}/{path}"
                r = self._client.request(method, url, headers=h, content=json.dumps(payload) if payload is not None and method in ("POST", "PATCH", "PUT") else None)
                if r.status_code == 403 and attempt == 1 and method != "GET":
                    self._fetch_csrf(service)
                    continue
                break
            try:
                body = r.json() if r.content else {}
            except ValueError:
                body = {"raw": r.text[:500]}
            return ApiResponse(r.status_code, body, {k.lower(): v for k, v in r.headers.items()})

    def _soap(self, operation: str, payload: dict) -> ApiResponse:
        env = journal_entry_envelope(operation, payload)
        r = self._client.post(f"{self.base}/sap/bc/srt/scs_ext/sap/journalentrybulkcreaterequestco", headers={**self._auth_headers(), "Content-Type": "text/xml; charset=utf-8", "SOAPAction": operation}, content=env)
        text = r.text
        if r.status_code >= 400 or "<faultstring>" in text:
            m = re.search(r"<faultstring>(.*?)</faultstring>", text, re.S)
            return ApiResponse(r.status_code if r.status_code >= 400 else 500, {"error": {"code": "SOAP_FAULT", "message": {"value": (m.group(1) if m else text[:300])}}})
        doc = re.search(r"<AccountingDocument>(\d+)</AccountingDocument>", text)
        fy = re.search(r"<FiscalYear>(\d+)</FiscalYear>", text)
        return ApiResponse(201, {"d": {"CompanyCode": payload.get("CompanyCode"), "AccountingDocument": doc.group(1) if doc else None, "FiscalYear": int(fy.group(1)) if fy else payload.get("FiscalYear"), "numbering": "internal"}})


def journal_entry_envelope(operation: str, payload: dict) -> str:
    """SOAP envelope for JournalEntryBulkCreateRequestConfirmation_In (one entry) or a reversal request."""

    def tag(name: str, value: Any) -> str:
        if value is None or value == "":
            return ""
        v = str(value).replace("&", "&amp;").replace("<", "&lt;")
        return f"<{name}>{v}</{name}>"

    if operation == "JournalEntryReverse":
        body = "<JournalEntryReverse>" + "".join(tag(k, v) for k, v in payload.items()) + "</JournalEntryReverse>"
    else:
        items = "".join("<Item>" + "".join(tag(k, v) for k, v in it.items()) + "</Item>" for it in payload.get("Items", []))
        header = "".join(tag(k, v) for k, v in payload.items() if k != "Items")
        body = f"<JournalEntryBulkCreateRequest><MessageHeader><CreationDateTime>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}</CreationDateTime></MessageHeader><JournalEntryCreateRequest><JournalEntry>{header}{items}</JournalEntry></JournalEntryCreateRequest></JournalEntryBulkCreateRequest>"
    return f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>{body}</soap:Body></soap:Envelope>'


def make_target_transport(session, target) -> SimulatedS4Gateway | S4ApiHttpTransport:
    """SYNTHETIC targets and API targets on the simulated transport get the gateway; API targets with an HTTP
    destination get the real transport."""
    api = (target.meta or {}).get("api", {}) or {}
    mode = (api.get("transport") or os.getenv("SDTF_S4_API_TRANSPORT", "auto")).lower()
    if target.connector == "SYNTHETIC" or mode == "simulated":
        return SimulatedS4Gateway(session, target.id, numbering=api.get("numbering"))
    dest = resolve_api_destination(target.sid, target.meta)
    if mode == "http" or (mode == "auto" and dest):
        if not dest:
            raise ApiUnavailable(f"no API destination for {target.sid}: set SDTF_S4_API_{target.sid.upper()} (JSON with base_url, user/passwd or token_url/client_id/client_secret) or meta.api.dest")
        return S4ApiHttpTransport(dest)
    raise ApiUnavailable(f"no API destination configured for {target.sid}; set SDTF_S4_API_{target.sid.upper()} or register the target with meta.api.transport='simulated'")


# ---------------------------------------------------------------------------------------------------- client
class TargetApiClient:
    """Typed calls over a transport with call accounting; raises ApiError on any non-success status."""

    def __init__(self, transport):
        self.t = transport
        self._tokens: dict[str, str] = {}
        self.calls: Counter_ = Counter_()
        self.failures: Counter_ = Counter_()

    def _token(self, service: str) -> str:
        if service not in self._tokens:
            r = self.t.request("GET", service, "", None, {"x-csrf-token": "fetch"})
            if r.status >= 400:
                raise ApiError(r.status, "CSRF_FETCH", str(r.body)[:200])
            self._tokens[service] = r.headers.get("x-csrf-token", "")
        return self._tokens[service]

    def _call(self, method: str, service: str, path: str, payload: dict | None = None, headers: dict | None = None) -> ApiResponse:
        h = dict(headers or {})
        if method != "GET" and service != "API_JOURNALENTRY_SRV":
            h["x-csrf-token"] = self._token(service)
        r = self.t.request(method, service, path, payload, h)
        if r.status == 403 and method != "GET":  # token expired: refresh once
            self._tokens.pop(service, None)
            h["x-csrf-token"] = self._token(service)
            r = self.t.request(method, service, path, payload, h)
        self.calls[(service, method, path.split("(")[0])] += 1
        if r.status >= 400:
            self.failures[(service, method, path.split("(")[0])] += 1
            err = (r.body or {}).get("error", {})
            msg = err.get("message", {})
            raise ApiError(r.status, err.get("code", "HTTP"), msg.get("value") if isinstance(msg, dict) else (msg or str(r.body)[:200]))
        return r

    def get(self, service: str, entity_set: str, props: tuple[str, ...], values: list[str]) -> tuple[dict | None, str | None]:
        try:
            r = self._call("GET", service, entity_set + format_key(props, values))
        except ApiError as e:
            if e.status == 404:
                self.failures[(service, "GET", entity_set)] -= 1
                return None, None
            raise
        return r.body.get("d"), r.headers.get("etag")

    def create(self, service: str, entity_set: str, entity: dict) -> dict:
        return self._call("POST", service, entity_set, entity).body.get("d", {})

    def update(self, service: str, entity_set: str, props: tuple[str, ...], values: list[str], patch: dict, etag: str | None) -> None:
        self._call("PATCH", service, entity_set + format_key(props, values), patch, {"If-Match": etag or "*"})

    def delete(self, service: str, entity_set: str, props: tuple[str, ...], values: list[str], etag: str | None) -> None:
        self._call("DELETE", service, entity_set + format_key(props, values), None, {"If-Match": etag or "*"})

    def post_journal_entry(self, entry: dict) -> dict:
        return self._call("POST", "API_JOURNALENTRY_SRV", "JournalEntryBulkCreateRequestConfirmation_In", entry).body.get("d", {})

    def reverse_journal_entry(self, company_code: str, document: str, fiscal_year, posting_date: str | None = None) -> dict:
        return self._call("POST", "API_JOURNALENTRY_SRV", "JournalEntryReverse", {"CompanyCode": company_code, "AccountingDocument": document, "FiscalYear": fiscal_year, "PostingDate": posting_date, "ReversalReason": "01"}).body.get("d", {})

    def stats(self) -> dict:
        by_service: dict[str, int] = {}
        for (s, _m, _p), n in self.calls.items():
            by_service[s] = by_service.get(s, 0) + n
        return {"calls": sum(self.calls.values()), "by_service": by_service, "by_operation": {f"{s} {m} {p}": n for (s, m, p), n in sorted(self.calls.items())}, "failures": sum(self.failures.values())}


from collections import Counter as Counter_  # noqa: E402

"""Reconciliation through the adapters: source and target views read from the systems, not from the record store.

`reconcile_run` works on two `RecordStore`-shaped views. Where they come from depends on the connector:

* **Source over RFC** (`RfcSourceView`): the financial tables the reconciliation needs (T001K, BKPF, BSEG, BSID,
  BSIK, ANLC, MBEW) are read through the add-on (`Z_SDTF_READ_PACKAGE`) with the scope's company codes pushed down
  (valuation areas for MBEW), under one snapshot token. `Z_SDTF_AGGREGATE` gives the row count per table before
  the read (a guard against reading a whole BSEG by mistake: `SDTF_RECON_MAX_ROWS`) and the debit/credit totals
  per company code computed in the source database; both are compared with what arrived and reported as
  TECHNICAL checks `source_read_integrity` / `source_read_amounts`, so a truncated or inconsistent read can never
  pass as a reconciliation. On the simulated add-on the rows are the record store's, so results equal the former
  direct read; on a real system this is the first path that reads the source for reconciliation at all.
* **Target over the released APIs** (`ApiTargetView`): the loaded records are read back by key through the
  entity bindings (`GET A_SalesOrder('...')`), tables with a company code property are read as filtered
  collections (`$filter=CompanyCode eq ...`, paged), journal entries come from `API_JOURNALENTRYITEMBASIC_SRV`
  (header, line and open-item images derived from the line items), referenced masters missing from the view are
  fetched lazily; asset values come from the fixed-asset read service and material valuation from the product
  valuation entity (read-only bindings in `catalog/api_bindings.py`). Tables no released read API covers (T001,
  T001K, asset values when the read service is absent, loaded history tables such as EKBE) are then read through
  the **read-only add-on over RFC on the target** (`meta.rfc` on the target system: the same `Z_SDTF_*` modules an
  on-premise S/4HANA can host), with company-code pushdown or the loaded keys pushed down; what still has no read
  path is reported as `unreadable`: checks that need it say so (WARN) instead of failing on an empty table.
* Record-store systems (SYNTHETIC, simulated gateway) keep the direct read: the simulated gateway writes the
  record store, so that is what "the API" holds.

Nothing here is verified against a live SAP system; the property names of the journal item service are taken
from the public API reference and must be checked against the target's `$metadata` (ADR-0016).
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Iterable

from sqlalchemy.orm import Session

from ..catalog.api_bindings import API_BINDINGS, READ_BINDINGS, READ_SERVICE_OF, EntityBinding
from ..catalog.store import RecordStore
from ..catalog.tables import TABLES, record_key
from ..models import ReconciliationResult, SapSystem
from ..runtime.rfc import AbapAddonClient, RfcError, make_transport, predicate
from ..runtime.target_api import ApiError, TargetApiClient, make_target_transport

SOURCE_TABLES = ("T001K", "BKPF", "BSEG", "BSID", "BSIK", "ANLC", "MBEW")
JOURNAL_SERVICE = "API_JOURNALENTRYITEMBASIC_SRV"
JOURNAL_ENTITY = "A_JournalEntryItemBasic"
# A_JournalEntryItemBasic property -> table field (public API reference; unverified against a target's $metadata)
JOURNAL_FIELDS = {"CompanyCode": "BUKRS", "AccountingDocument": "BELNR", "FiscalYear": "GJAHR", "AccountingDocumentItem": "BUZEI", "LedgerGLLineItem": "BUZEI", "FinancialAccountType": "KOART", "DebitCreditCode": "SHKZG", "GLAccount": "HKONT", "AmountInCompanyCodeCurrency": "DMBTR", "AmountInTransactionCurrency": "WRBTR", "Customer": "KUNNR", "Supplier": "LIFNR", "CostCenter": "KOSTL", "ProfitCenter": "PRCTR", "ClearingAccountingDocument": "AUGBL", "ClearingDate": "AUGDT", "PartnerCompany": "VBUND", "Material": "MATNR", "Plant": "WERKS", "SpecialGLCode": "UMSKZ", "AssignmentReference": "ZUONR"}
JOURNAL_HEADER_FIELDS = {"AccountingDocumentType": "BLART", "DocumentDate": "BLDAT", "PostingDate": "BUDAT", "FiscalPeriod": "MONAT", "TransactionCurrency": "WAERS", "OriginalReferenceDocumentType": "AWTYP", "OriginalReferenceDocument": "AWKEY", "DocumentReferenceID": "XBLNR"}
UNREADABLE_BY_API = ("T001", "T001K")  # no released read service bound in this build
PAGE = 1000


def max_rows() -> int:
    return int(os.getenv("SDTF_RECON_MAX_ROWS", "5000000"))


def recon_mode() -> str:
    """rows: read the scope's financial rows through the add-on; aggregate: totals computed in the source, only the
    retained documents' lines transferred; auto: aggregate when the scope's BSEG exceeds SDTF_RECON_AGGREGATE_ABOVE."""
    return os.getenv("SDTF_RECON_MODE", "auto").lower()


def aggregate_above() -> int:
    return int(os.getenv("SDTF_RECON_AGGREGATE_ABOVE", "1000000"))


MODES = ("auto", "rows", "aggregate")


class ReconciliationViewError(Exception):
    """The view could not be built (scope too large for a row read, adapter failure)."""


def _r(run_id, layer, name, status, subject="", src="", tgt="", variance="", explanation="", evidence=None):
    return ReconciliationResult(run_id=run_id, layer=layer, check_name=name, subject=subject, status=status, source_value=str(src), target_value=str(tgt), variance=str(variance), explanation=explanation, evidence=evidence or {})


def normalise(value) -> str:
    """Comparable form of a field value across the staging image and an API read (numbers as 2-decimal strings,
    None as empty): the read services return typed JSON, the staging keeps the extracted strings."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "X" if value else ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value).strip()


class ViewStore(RecordStore):
    """A RecordStore filled through an adapter, with the tables it could not read, the tables it derives, the
    fields a table can be compared on, and the integrity evidence."""

    def __init__(self, system_id: str, origin: str):
        super().__init__(system_id)
        self.origin = origin
        self.unreadable: set[str] = set()
        self.derived_tables: set[str] = set()  # present as projections (open items from journal lines): totals, not rows
        self.metrics: dict = {"origin": origin}
        self._integrity: list[tuple] = []
        self._comparable: dict[str, set[str]] = {}

    def comparable_fields(self, table: str) -> set[str] | None:
        """Fields a staged row can be compared with on this view (None: every field, as for a record store)."""
        return self._comparable.get(table)

    def integrity_results(self, run_id: str) -> list[ReconciliationResult]:
        out = []
        for name, subject, expected, actual, note in self._integrity:
            ok = expected == actual
            out.append(_r(run_id, "TECHNICAL", name, "PASS" if ok else "FAIL", subject, expected, actual, (expected - actual) if isinstance(expected, (int, float)) and isinstance(actual, (int, float)) else "", note if ok else f"{note}: the rows read through the adapter do not match the totals the source computed", {"origin": self.origin}))
        return out


def record_store_view(session: Session, system_id: str, origin: str = "record_store") -> ViewStore:
    base = RecordStore.load(session, system_id)
    v = ViewStore(system_id, origin)
    v._tables, v._by_key = base._tables, base._by_key
    v.metrics["rows"] = sum(len(r) for r in base._tables.values())
    return v


# ======================================================================================= source (RFC add-on)
def _uses_record_store(system: SapSystem) -> bool:
    if system.connector == "SYNTHETIC":
        return True
    if system.connector == "RFC":
        return False  # the add-on path, simulated or pyrfc: one code path for both
    if system.connector == "API":
        return ((system.meta or {}).get("api") or {}).get("transport", os.getenv("SDTF_S4_API_TRANSPORT", "auto")).lower() == "simulated"
    return True


def build_source_view(session: Session, source: SapSystem, manifest, limit: int | None = None, mode: str | None = None) -> ViewStore:
    """The source side of the reconciliation for `manifest`'s company codes, read through the source's adapter.
    `mode`: rows | aggregate | auto (default from SDTF_RECON_MODE)."""
    mode = (mode or recon_mode()).lower()
    if mode not in MODES:
        raise ReconciliationViewError(f"mode must be one of {', '.join(MODES)}")
    if _uses_record_store(source):
        return record_store_view(session, source.id)
    if source.connector != "RFC":
        raise ReconciliationViewError(f"no reconciliation read path for connector {source.connector}")
    transport = make_transport(source.sid, source.meta, store_loader=lambda: RecordStore.load(session, source.id))
    client = AbapAddonClient(transport)
    scope_ccs = sorted(set(manifest.definition["company_codes"]))
    limit = limit or max_rows()
    client.open_snapshot(list(SOURCE_TABLES))
    cc_preds = [predicate("BUKRS", "EQ", cc) for cc in scope_ccs]
    decision = {"requested": mode, "reason": "requested"}
    if mode != "rows":
        try:
            n_bseg = client.count("BSEG", cc_preds)
        except RfcError as e:
            if e.key not in ("FU_NOT_FOUND", "NOT_AUTHORIZED"):
                raise
            if mode == "aggregate":
                raise ReconciliationViewError(f"aggregate-only reconciliation needs Z_SDTF_AGGREGATE in the add-on ({e.key})") from None
            n_bseg = None
            decision["reason"] = "Z_SDTF_AGGREGATE unavailable: rows"
        if n_bseg is not None:
            decision["bseg_rows"] = n_bseg
            if mode == "aggregate" or n_bseg > aggregate_above():
                decision["reason"] = "requested" if mode == "aggregate" else f"BSEG {n_bseg} rows above SDTF_RECON_AGGREGATE_ABOVE={aggregate_above()}"
                return _aggregate_source_view(session, source, manifest, client, transport, scope_ccs, cc_preds, decision)
            decision["reason"] = f"BSEG {n_bseg} rows within SDTF_RECON_AGGREGATE_ABOVE={aggregate_above()}: rows"
    view = ViewStore(source.id, "rfc_addon")
    view.metrics["mode"] = "rows"
    view.metrics["mode_decision"] = decision
    aggregate_available = True
    by_table: dict[str, int] = {}

    def read(table: str, preds: list[dict]) -> None:
        nonlocal aggregate_available
        expected = None
        if aggregate_available:
            try:
                expected = client.count(table, preds)
            except RfcError as e:
                if e.key not in ("FU_NOT_FOUND", "NOT_AUTHORIZED"):
                    raise
                aggregate_available = False  # add-on without Z_SDTF_AGGREGATE: rows only, no integrity evidence
        if expected is not None and expected > limit:
            raise ReconciliationViewError(f"{table} holds {expected} rows for company codes {', '.join(scope_ccs)}, above SDTF_RECON_MAX_ROWS={limit}; narrow the scope or raise the limit")
        try:
            n = view.add_rows(table, client.read_all(table, preds))
        except RfcError as e:
            if e.key in ("NOT_AUTHORIZED", "TABLE_UNKNOWN"):
                view.unreadable.add(table)
                view.metrics.setdefault("errors", {})[table] = str(e)
                return
            raise
        by_table[table] = n
        if expected is not None:
            view._integrity.append(("source_read_integrity", table, expected, n, "row count computed in the source vs rows read"))

    read("T001K", cc_preds)
    areas = sorted({str(r["BWKEY"]) for r in view.rows("T001K")})
    for t in ("BKPF", "BSEG", "BSID", "BSIK", "ANLC"):
        read(t, cc_preds)
    if areas:
        read("MBEW", [predicate("BWKEY", "EQ", a) for a in areas])
    if aggregate_available and "BSEG" not in view.unreadable:
        try:
            agg = client.aggregate("BSEG", cc_preds, ["BUKRS", "SHKZG"], ["DMBTR"])
            got: dict[tuple[str, str], float] = defaultdict(float)
            for l in view.rows("BSEG"):
                got[(str(l["BUKRS"]), str(l["SHKZG"]))] += float(l["DMBTR"] or 0)
            for a in agg:
                k = (str(a["BUKRS"]), str(a["SHKZG"]))
                view._integrity.append(("source_read_amounts", f"{k[0]}/{k[1]}", round(float(a["SUM_DMBTR"]), 2), round(got.get(k, 0.0), 2), "DMBTR total computed in the source vs rows read"))
        except RfcError as e:
            view.metrics.setdefault("errors", {})["aggregate"] = str(e)
    view.metrics.update({"transport": getattr(transport, "name", "?"), "snapshot": client.snapshot, "company_codes": scope_ccs, "valuation_areas": areas, "rfc_calls": client.calls, "packages": client.packages, "rows": sum(by_table.values()), "by_table": by_table, "aggregate_available": aggregate_available, "unreadable": sorted(view.unreadable), "limit": limit})
    return view


LEADING_LEDGER = "0L"


def journal_table_for(system: SapSystem, client: AbapAddonClient) -> tuple[str, str]:
    """Which journal the aggregates come from on this system: ACDOCA (Universal Journal, leading ledger) on S/4HANA
    when the add-on can read it and it holds rows, else BSEG. `meta.rfc.journal_table` forces one, `meta.rfc.ledger`
    picks the ledger (default 0L)."""
    rfc = (system.meta or {}).get("rfc") or {}
    ledger = str(rfc.get("ledger") or LEADING_LEDGER)
    forced = str(rfc.get("journal_table") or "").upper()
    if forced in ("ACDOCA", "BSEG"):
        return forced, ledger
    if (system.product or "").upper() != "S4HANA":
        return "BSEG", ledger
    try:
        md = client.table_metadata("ACDOCA")
        return ("ACDOCA" if md.get("authorized", True) and md.get("rows", 0) > 0 else "BSEG"), ledger
    except RfcError:
        return "BSEG", ledger


def _acdoca_rows(rows: list[dict], group_map: dict[str, str]) -> list[dict]:
    """ACDOCA aggregate rows (signed HSL/WSL per DRCRK) in the BSEG form the checks use: BUKRS/HKONT/SHKZG...
    with positive magnitudes SUM_DMBTR / SUM_WRBTR."""
    out = []
    for a in rows:
        r = {group_map.get(k, k): v for k, v in a.items() if k in group_map}
        sign = -1.0 if r.get("SHKZG") == "H" else 1.0
        r["COUNT"] = a["COUNT"]
        if "SUM_HSL" in a:
            r["SUM_DMBTR"] = round(sign * float(a["SUM_HSL"]), 2)
        if "SUM_WSL" in a:
            r["SUM_WRBTR"] = round(sign * float(a["SUM_WSL"]), 2)
        out.append(r)
    return out


ASSET_DEFAULTS = {"source": "auto", "area": "01", "apc_movement_categories": [], "apc_tables": ["ACDOCA", "FAAT_DOC_IT"]}


def asset_config(system: SapSystem | None) -> dict:
    """`meta.rfc.assets`: how acquisition values are read on an S/4HANA system. `source`: auto | faav_anlc |
    apc_items | net; `area`: depreciation area (default 01); `apc_movement_categories`: FAA_MOVCAT values that
    carry acquisition and production costs on this system (none known in this build: take them from the domain
    on the system); `apc_tables`: where the APC line items are (posting areas: ACDOCA; others: FAAT_DOC_IT)."""
    cfg = dict(ASSET_DEFAULTS)
    rfc = (system.meta or {}).get("rfc") if system is not None and system.meta else None
    cfg.update({k: v for k, v in ((rfc or {}).get("assets") or {}).items() if k in cfg})
    cfg["apc_movement_categories"] = [str(x) for x in cfg["apc_movement_categories"] or []]
    cfg["apc_tables"] = [str(t).upper() for t in cfg["apc_tables"] or []]
    return cfg


def asset_values(client: AbapAddonClient, company_codes: list[str], ledger: str, cfg: dict) -> tuple[list[dict], str, bool]:
    """Acquisition values of an S/4HANA system's company codes, per company code: `([{BUKRS, SUM_KANSW}], measure,
    comparable)`. Chain: the compatibility view FAAV_ANLC (classic ANLC semantics: comparable with ECC acquisition
    values), else the APC line items of ACDOCA and FAAT_DOC_IT filtered by movement category (comparable, needs the
    categories configured), else the net asset postings of the Universal Journal (a different measure: not
    comparable, reported as such)."""
    want = cfg["source"]
    area = str(cfg["area"])
    cc_preds = [predicate("BUKRS", "EQ", cc) for cc in company_codes]
    if want in ("auto", "faav_anlc"):
        try:
            rows = client.aggregate("FAAV_ANLC", cc_preds + [predicate("AFABE", "EQ", area)], ["BUKRS"], ["KANSW"])
            if rows or want == "faav_anlc":
                return [{"BUKRS": cc, "SUM_KANSW": round(sum(float(r["SUM_KANSW"]) for r in rows if r["BUKRS"] == cc), 2)} for cc in company_codes], f"acquisition values from the compatibility view FAAV_ANLC, depreciation area {area}", True
        except RfcError as e:
            if want == "faav_anlc":
                raise ReconciliationViewError(f"FAAV_ANLC not readable on the target: {e}") from e
    if want in ("auto", "apc_items") and cfg["apc_movement_categories"]:
        totals: dict[str, float] = {cc: 0.0 for cc in company_codes}
        used = []
        for table in cfg["apc_tables"]:
            cc_field = "RBUKRS" if table == "ACDOCA" else "BUKRS"
            preds = [predicate(cc_field, "EQ", cc) for cc in company_codes] + [predicate("AFABE", "EQ", area), predicate("ANLN1", "NE", "")] + [predicate("MOVCAT", "EQ", m) for m in cfg["apc_movement_categories"]]
            if table == "ACDOCA":
                preds.append(predicate("RLDNR", "EQ", ledger))
            try:
                rows = client.aggregate(table, preds, [cc_field, "DRCRK"], ["HSL"])
            except RfcError as e:
                if want == "apc_items":
                    raise ReconciliationViewError(f"{table} not readable on the target: {e}") from e
                continue
            used.append(table)
            for r in rows:
                totals[str(r[cc_field])] = totals.get(str(r[cc_field]), 0.0) + float(r["SUM_HSL"])  # HSL is signed: acquisitions positive, retirements negative
        if used:
            return [{"BUKRS": cc, "SUM_KANSW": round(v, 2)} for cc, v in totals.items()], f"acquisition and production costs from the asset line items of {', '.join(used)} (depreciation area {area}, movement categories {', '.join(cfg['apc_movement_categories'])})", True
    if want == "apc_items":
        raise ReconciliationViewError("apc_items needs meta.rfc.assets.apc_movement_categories (the FAA_MOVCAT values that carry acquisition and production costs on this system)")
    base = [predicate("RLDNR", "EQ", ledger)] + [predicate("RBUKRS", "EQ", cc) for cc in company_codes] + [predicate("KOART", "EQ", "A"), predicate("ANLN1", "NE", "")]
    rows = client.aggregate("ACDOCA", base, ["RBUKRS", "DRCRK"], ["HSL"])
    net = [{"BUKRS": cc, "SUM_KANSW": round(sum(float(r["SUM_HSL"]) for r in rows if r["RBUKRS"] == cc), 2)} for cc in company_codes]
    return net, "net asset postings in the Universal Journal (account type A, asset assigned): APC less accumulated depreciation, not the acquisition value", False


def journal_aggregates(client: AbapAddonClient, company_codes: list[str], journal_table: str, ledger: str, valuation_areas: list[str], system: SapSystem | None = None) -> dict:
    """The totals the financial layer needs, computed in the system: from ACDOCA (Universal Journal, one ledger,
    signed amounts normalised) or from BSEG with the open-item tables. Same keys either way."""
    A: dict = {"journal_table": journal_table, "ledger": ledger if journal_table == "ACDOCA" else None}
    if journal_table == "ACDOCA":
        base = [predicate("RLDNR", "EQ", ledger)] + [predicate("RBUKRS", "EQ", cc) for cc in company_codes]
        open_pred = [predicate("AUGBL", "EQ", "")]
        g = {"RBUKRS": "BUKRS", "RACCT": "HKONT", "DRCRK": "SHKZG", "KOART": "KOART", "RASSC": "VBUND", "GJAHR": "GJAHR"}
        A["counts"] = {"ACDOCA": client.count("ACDOCA", base), "BKPF": client.count("BKPF", [predicate("BUKRS", "EQ", cc) for cc in company_codes])}
        A["gl"] = _acdoca_rows(client.aggregate("ACDOCA", base, ["RBUKRS", "RACCT", "DRCRK"], ["HSL"]), g)
        A["totals"] = _acdoca_rows(client.aggregate("ACDOCA", base, ["RBUKRS", "DRCRK"], ["HSL", "WSL"]), g)
        open_items = _acdoca_rows(client.aggregate("ACDOCA", base + open_pred + [predicate("KOART", "EQ", "D"), predicate("KOART", "EQ", "K")], ["RBUKRS", "KOART", "DRCRK"], ["HSL"]), g)
        A["open_ar"] = [a for a in open_items if a["KOART"] == "D"]
        A["open_ap"] = [a for a in open_items if a["KOART"] == "K"]
        A["intercompany"] = _acdoca_rows(client.aggregate("ACDOCA", base + open_pred + [predicate("RASSC", "NE", "")], ["RBUKRS", "RASSC", "KOART", "DRCRK"], ["HSL"]), g)
        A["assets"], A["assets_measure"], A["assets_comparable"] = asset_values(client, company_codes, ledger, asset_config(system))
    else:
        cc_preds = [predicate("BUKRS", "EQ", cc) for cc in company_codes]
        open_pred = [predicate("AUGBL", "EQ", "")]
        A["counts"] = {t: client.count(t, cc_preds) for t in ("BKPF", "BSEG", "BSID", "BSIK", "ANLC")}
        A["gl"] = client.aggregate("BSEG", cc_preds, ["BUKRS", "HKONT", "SHKZG"], ["DMBTR"])
        A["totals"] = client.aggregate("BSEG", cc_preds, ["BUKRS", "SHKZG"], ["DMBTR", "WRBTR"])
        A["open_ar"] = client.aggregate("BSID", cc_preds + open_pred, ["BUKRS", "SHKZG"], ["DMBTR"])
        A["open_ap"] = client.aggregate("BSIK", cc_preds + open_pred, ["BUKRS", "SHKZG"], ["DMBTR"])
        A["intercompany"] = client.aggregate("BSEG", cc_preds + open_pred + [predicate("VBUND", "NE", "")], ["BUKRS", "VBUND", "KOART", "SHKZG"], ["DMBTR"])
        A["assets"] = client.aggregate("ANLC", cc_preds, ["BUKRS"], ["KANSW"])
    A["documents_by_year"] = client.aggregate("BKPF", [predicate("BUKRS", "EQ", cc) for cc in company_codes], ["BUKRS", "GJAHR"], [])
    A["inventory"] = client.aggregate("MBEW", [predicate("BWKEY", "EQ", a) for a in valuation_areas], ["BWKEY"], ["SALK3"]) if valuation_areas else []
    return A


def acdoca_from_journal(bkpf: list[dict], bseg: list[dict], ledger: str = LEADING_LEDGER) -> list[dict]:
    """Universal Journal rows derived from classic headers and lines (one ledger, signed amounts): what an
    S/4HANA system holds for the same postings. Used to prepare simulated S/4HANA targets and in tests; the mapping
    documents how ACDOCA aggregates are read back into the BSEG form."""
    heads = {(h["BUKRS"], h["BELNR"], str(h["GJAHR"])): h for h in bkpf}
    out = []
    for l in bseg:
        h = heads.get((l["BUKRS"], l["BELNR"], str(l["GJAHR"]))) or {}
        sign = -1.0 if l.get("SHKZG") == "H" else 1.0
        out.append({"RLDNR": ledger, "RBUKRS": l["BUKRS"], "GJAHR": str(l["GJAHR"]), "BELNR": l["BELNR"], "DOCLN": str(l["BUZEI"]).zfill(6), "RACCT": l.get("HKONT", ""), "DRCRK": l.get("SHKZG", "S"), "HSL": round(sign * float(l.get("DMBTR") or 0), 2), "WSL": round(sign * float(l.get("WRBTR") or 0), 2), "RHCUR": h.get("WAERS", ""), "RWCUR": h.get("WAERS", ""), "KOART": l.get("KOART", ""), "KUNNR": l.get("KUNNR", ""), "LIFNR": l.get("LIFNR", ""), "AUGBL": l.get("AUGBL", ""), "AUGDT": l.get("AUGDT", ""), "RASSC": l.get("VBUND", ""), "MATNR": l.get("MATNR", ""), "WERKS": l.get("WERKS", ""), "RCNTR": l.get("KOSTL", ""), "PRCTR": l.get("PRCTR", ""), "ANLN1": l.get("ANLN1", ""), "ANLN2": l.get("ANLN2", ""), "BUDAT": h.get("BUDAT", ""), "BLART": h.get("BLART", ""), "POPER": str(h.get("MONAT", "")).zfill(3) if h.get("MONAT") not in (None, "") else "", "AWTYP": h.get("AWTYP", ""), "AWREF": h.get("AWKEY", ""), "BSTAT": h.get("BSTAT", "")})
    return out


class AggregateSourceView(ViewStore):
    """The source side as totals computed in the source database (Z_SDTF_AGGREGATE), for scopes whose line items
    are too many to transfer for a reconciliation. Holds T001K rows, the aggregates the financial layer needs and
    the line items of the documents the scope retains (read by key, bounded by the classification). `rows()` of
    the large tables is empty on purpose: `aggregate_only` tells the checks to use `aggregates` instead."""

    aggregate_only = True

    def __init__(self, system_id: str):
        super().__init__(system_id, "rfc_aggregate")
        self.aggregates: dict = {}
        self.retained_lines: list[dict] = []


def _chunks(values, n):
    vals = sorted(set(values))
    for i in range(0, len(vals), n):
        yield vals[i : i + n]


def _aggregate_source_view(session: Session, source: SapSystem, manifest, client: AbapAddonClient, transport, scope_ccs: list[str], cc_preds: list[dict], decision: dict) -> AggregateSourceView:
    from ..runtime import rfc_config

    view = AggregateSourceView(source.id)
    view.metrics.update({"mode": "aggregate", "mode_decision": decision})
    rows_transferred = view.add_rows("T001K", client.read_all("T001K", cc_preds))
    areas = sorted({str(r["BWKEY"]) for r in view.rows("T001K")})
    area_preds = [predicate("BWKEY", "EQ", a) for a in areas]
    journal_table, ledger = journal_table_for(source, client)
    view.aggregates = journal_aggregates(client, scope_ccs, journal_table, ledger, areas, source)
    A = view.aggregates
    cls = manifest.selection.get("classification", {})
    not_transferred = [n.split(":", 1)[1] for n, c in cls.items() if c["type"] == "MD.Material" and c["classification"] not in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")]
    held = 0.0
    if areas and not_transferred:
        for chunk in _chunks(not_transferred, rfc_config.key_chunk()):
            for a in client.aggregate("MBEW", area_preds + [predicate("MATNR", "EQ", m) for m in chunk], [], ["SALK3"]):
                held += float(a["SUM_SALK3"])
    A["inventory_held"] = round(held, 2)
    # the retained documents' lines, by key: the only line items that cross the wire
    retained = [n.split(":", 1)[1] for n, c in cls.items() if c["type"] == "FI.AccountingDocument" and c["classification"] not in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")]
    by_cc_year: dict[tuple[str, str], list[str]] = defaultdict(list)
    for k in retained:
        parts = k.split("|")
        if len(parts) == 3 and parts[0] in scope_ccs:
            by_cc_year[(parts[0], parts[2])].append(parts[1])
    in_scope_retained = sum(len(d) for d in by_cc_year.values())
    for (cc, year), docs in sorted(by_cc_year.items()):
        for chunk in _chunks(docs, rfc_config.key_chunk()):
            lines = list(client.read_all("BSEG", [predicate("BUKRS", "EQ", cc), predicate("GJAHR", "EQ", year)] + [predicate("BELNR", "EQ", d) for d in chunk]))
            view.retained_lines.extend(lines)
            rows_transferred += len(lines)
    # evidence: the source balances in its own books, per company code
    for cc in scope_ccs:
        debit = round(sum(float(a["SUM_DMBTR"]) for a in A["totals"] if a["BUKRS"] == cc and a["SHKZG"] == "S"), 2)
        credit = round(sum(float(a["SUM_DMBTR"]) for a in A["totals"] if a["BUKRS"] == cc and a["SHKZG"] == "H"), 2)
        view._integrity.append(("source_trial_balance", cc, debit, credit, "debits and credits of the source company code computed in the source database"))
    view.metrics.update({"transport": getattr(transport, "name", "?"), "snapshot": client.snapshot, "journal_table": journal_table, "ledger": A.get("ledger"), "company_codes": scope_ccs, "valuation_areas": areas, "rfc_calls": client.calls, "packages": client.packages, "rows": rows_transferred, "rows_avoided": sum(A["counts"].values()), "retained_documents": in_scope_retained, "retained_documents_outside_scope": len(retained) - in_scope_retained, "retained_lines": len(view.retained_lines), "by_table": {"T001K": len(view.rows("T001K")), "BSEG(retained)": len(view.retained_lines)}, "aggregate_available": True, "unreadable": [], "counts": A["counts"]})
    return view


# ===================================================================================== target (released APIs)
_BOUND: dict[str, tuple[str, EntityBinding]] = {}
for _b in API_BINDINGS.values():
    if _b.protocol == "ODATA_V2":
        _BOUND[_b.header.table] = (_b.service, _b.header)
        for _it in _b.items.values():
            _BOUND[_it.table] = (_b.service, _it)
for _t, _eb in READ_BINDINGS.items():  # read-only services: reconciliation reads, never loads
    _BOUND.setdefault(_t, (READ_SERVICE_OF[_t], _eb))
_FILTER_RE = re.compile(r"^\s*(?P<prop>[A-Za-z0-9_]+)\s+eq\s+'(?P<val>(?:[^']|'')*)'\s*$")


def odata_filter(prop: str, values: Iterable[str]) -> str:
    return " or ".join(f"{prop} eq '{str(v).replace(chr(39), chr(39) * 2)}'" for v in values)


def eval_filter(expr: str, entity: dict) -> bool:
    """Minimal OData V2 `$filter` evaluator for `Prop eq 'v'` terms joined by `and` / `or` (used by the simulated
    gateway; a real target evaluates the same expressions)."""
    expr = expr.strip()
    if not expr:
        return True
    for part in re.split(r"\s+or\s+", expr):
        ok = True
        for term in re.split(r"\s+and\s+", part):
            m = _FILTER_RE.match(term.strip("() "))
            if not m:
                raise ValueError(f"unsupported $filter term {term!r}")
            if str(entity.get(m.group("prop"), "")) != m.group("val").replace("''", "'"):
                ok = False
                break
        if ok:
            return True
    return False


def journal_rows(items: list[dict]) -> dict[str, list[dict]]:
    """BKPF headers, BSEG lines and BSID/BSIK open items derived from journal entry line items of the read service."""
    bseg, bkpf, bsid, bsik = [], {}, [], []
    for it in items:
        line = {f: it.get(p) for p, f in JOURNAL_FIELDS.items() if p in it and it.get(p) is not None}
        if "BUZEI" not in line and it.get("LedgerGLLineItem") is not None:
            line["BUZEI"] = it["LedgerGLLineItem"]
        for f in ("BUKRS", "BELNR", "BUZEI"):
            line.setdefault(f, "")
        line["GJAHR"] = str(line.get("GJAHR", ""))
        amt = it.get("AmountInCompanyCodeCurrency")
        if amt is not None and "SHKZG" not in line:
            line["SHKZG"] = "S" if float(amt) >= 0 else "H"
        if amt is not None:
            line["DMBTR"] = abs(float(amt))
        if it.get("AmountInTransactionCurrency") is not None:
            line["WRBTR"] = abs(float(it["AmountInTransactionCurrency"]))
        bseg.append(line)
        hk = (line["BUKRS"], line["BELNR"], line["GJAHR"])
        if hk not in bkpf:
            bkpf[hk] = {"BUKRS": hk[0], "BELNR": hk[1], "GJAHR": hk[2], **{f: it.get(p) for p, f in JOURNAL_HEADER_FIELDS.items() if p in it}}
        if line.get("KOART") in ("D", "K") and not line.get("AUGBL"):
            oi = {"BUKRS": line["BUKRS"], "UMSKS": "", "UMSKZ": line.get("UMSKZ") or "", "AUGDT": "", "AUGBL": "", "ZUONR": line.get("ZUONR") or "", "GJAHR": line["GJAHR"], "BELNR": line["BELNR"], "BUZEI": line["BUZEI"], "DMBTR": line.get("DMBTR"), "SHKZG": line.get("SHKZG")}
            if line["KOART"] == "D":
                bsid.append({**oi, "KUNNR": line.get("KUNNR", "")})
            else:
                bsik.append({**oi, "LIFNR": line.get("LIFNR", "")})
    return {"BKPF": list(bkpf.values()), "BSEG": bseg, "BSID": bsid, "BSIK": bsik}


class ApiTargetView(ViewStore):
    """The target as the released APIs show it. Built eagerly for the tables the checks scan; `by_key` fetches
    a missing row lazily (a master the load did not create but the target already held)."""

    aggregate_only = False

    def __init__(self, session: Session, target: SapSystem, company_codes: Iterable[str], loaded_keys: dict[str, set[str]], valuation_areas: Iterable[str] = (), fiscal_years: tuple[int | None, int | None] = (None, None), rfc_client: AbapAddonClient | None = None, aggregate: bool = False):
        super().__init__(target.id, "api_readback")
        self.transport = make_target_transport(session, target)
        self.client = TargetApiClient(self.transport)
        self.system = target
        self.rfc = rfc_client
        self.aggregate_only = bool(aggregate)
        self.aggregates: dict = {}
        self.aggregated_tables: set[str] = set()
        if self.aggregate_only:
            if rfc_client is None:
                raise ReconciliationViewError("aggregate-only reconciliation on the target needs the read-only add-on on the target (meta.rfc)")
            self.origin = "api_rfc_aggregate"
        self.company_codes = sorted(set(company_codes))
        self.loaded_keys = loaded_keys
        self.valuation_areas = sorted(set(valuation_areas))
        self.fiscal_years = fiscal_years
        self.read_via: dict[str, str] = {}
        self._missing: set[tuple[str, str]] = set()
        self.unreadable = set(UNREADABLE_BY_API) | {t for t in TABLES if t not in _BOUND and t not in ("BKPF", "BSEG", "BSID", "BSIK")}  # no released read service bound
        self.derived_tables = {"BSID", "BSIK"}
        for table, (_service, eb) in _BOUND.items():
            self._comparable[table] = set(eb.fields) | set(eb.derived)
        self._comparable["BSEG"] = set(JOURNAL_FIELDS.values()) - {"UMSKZ", "ZUONR"}
        self._comparable["BKPF"] = {"BUKRS", "BELNR", "GJAHR"} | set(JOURNAL_HEADER_FIELDS.values())
        self._load()

    # -- reads
    def _collection(self, service: str, entity_set: str, flt: str) -> list[dict]:
        out: list[dict] = []
        skip = 0
        while True:
            path = f"{entity_set}?$filter={flt}&$top={PAGE}&$skip={skip}" if flt else f"{entity_set}?$top={PAGE}&$skip={skip}"
            r = self.client._call("GET", service, path)
            d = r.body.get("d", {}) or {}
            results = d.get("results", d if isinstance(d, list) else [])
            out.extend(results)
            if len(results) < PAGE:
                return out
            skip += len(results)

    def _entity_rows(self, eb: EntityBinding, entities: list[dict]) -> list[dict]:
        rows = []
        for e in entities:
            row = eb.to_row(e)
            for f, p in eb.fields.items():
                if f in eb.derived and p in e:
                    row[f] = e[p]
            rows.append(row)
        return rows

    def _load(self) -> None:
        by_table: dict[str, int] = {}
        cc_prop_tables = []
        for table, (service, eb) in _BOUND.items():
            cc_prop = eb.fields.get("BUKRS")
            if cc_prop and "BUKRS" not in eb.derived:
                cc_prop_tables.append(table)
                try:
                    ents = self._collection(service, eb.entity_set, odata_filter(cc_prop, self.company_codes))
                    by_table[table] = self.add_rows(table, self._entity_rows(eb, ents))
                except ApiError as e:
                    self.unreadable.add(table)
                    self.metrics.setdefault("errors", {})[table] = str(e)
        if self.valuation_areas and "MBEW" in _BOUND:
            service, eb = _BOUND["MBEW"]
            try:
                ents = self._collection(service, eb.entity_set, odata_filter(eb.fields["BWKEY"], self.valuation_areas))
                rows = self._entity_rows(eb, ents)
                by_table["MBEW"] = self.add_rows("MBEW", rows)
                if rows and not any(r.get("SALK3") is not None for r in rows):  # the entity exposes prices, not the stock value
                    self.unreadable.add("MBEW")
                    self.metrics.setdefault("errors", {})["MBEW"] = "product valuation entity carries no stock value (TotalValue): inventory values not readable"
            except ApiError as e:
                self.unreadable.add("MBEW")
                self.metrics.setdefault("errors", {})["MBEW"] = str(e)
        # loaded records read back by key (masters, documents without a company code property)
        for table, keys in sorted(self.loaded_keys.items()):
            if table in cc_prop_tables or table in ("BKPF", "BSEG", "BSID", "BSIK"):
                continue
            if table not in _BOUND:
                self.unreadable.add(table)
                continue
            n = 0
            for k in sorted(keys):
                if self._fetch(table, k) is not None:
                    n += 1
            by_table[table] = n
        # journal entries through the read service (aggregate mode: totals computed in the target instead, below)
        if not self.aggregate_only:
            flt = odata_filter("CompanyCode", self.company_codes)
            yf, yt = self.fiscal_years
            if yf or yt:
                years = [str(y) for y in range(yf or yt, (yt or yf) + 1)]
                flt = f"({flt}) and ({odata_filter('FiscalYear', years)})"
            try:
                items = self._collection(JOURNAL_SERVICE, JOURNAL_ENTITY, flt)
                for t, rows in journal_rows(items).items():
                    by_table[t] = self.add_rows(t, rows)
            except ApiError as e:
                self.unreadable |= {"BKPF", "BSEG", "BSID", "BSIK"}
                self.metrics.setdefault("errors", {})["journal"] = str(e)
        for t in by_table:
            self.read_via.setdefault(t, "api")
        if self.aggregate_only:
            self._rfc_aggregates(by_table)
        if self.rfc is not None:
            self._rfc_readback(by_table)
        self.metrics.update({"transport": getattr(self.transport, "name", "?"), "mode": "aggregate" if self.aggregate_only else "rows", "company_codes": self.company_codes, "valuation_areas": self.valuation_areas, "by_table": by_table, "rows": sum(by_table.values()), "unreadable": sorted(self.unreadable), "read_via": dict(self.read_via), **self.client.stats()})

    def _rfc_aggregates(self, by_table: dict[str, int]) -> None:
        """Totals computed in the target database through the add-on: the journal tables, open items, asset values
        and material valuation are never transferred; the financial layer compares totals, the technical layer says
        the loaded journal rows were compared as totals."""
        client = self.rfc
        try:
            client.open_snapshot(["BKPF", "BSEG", "BSID", "BSIK", "ANLC", "MBEW", "ACDOCA"])
            journal_table, ledger = journal_table_for(self.system, client)
            A = journal_aggregates(client, self.company_codes, journal_table, ledger, self.valuation_areas, self.system)
        except RfcError as e:
            self.metrics.setdefault("errors", {})["aggregate"] = str(e)
            raise ReconciliationViewError(f"aggregate-only reconciliation on the target failed: {e}") from e
        self.aggregates = A
        self.aggregated_tables = {"BKPF", "BSEG", "BSID", "BSIK", "ANLC", "MBEW"}
        self.unreadable -= self.aggregated_tables
        for t in self.aggregated_tables:
            self.read_via[t] = "rfc_aggregate"
            self._tables.pop(t, None)
            self._by_key.pop(t, None)
            by_table.pop(t, None)
        for cc in self.company_codes:
            debit = round(sum(float(a["SUM_DMBTR"]) for a in A["totals"] if a["BUKRS"] == cc and a["SHKZG"] == "S"), 2)
            credit = round(sum(float(a["SUM_DMBTR"]) for a in A["totals"] if a["BUKRS"] == cc and a["SHKZG"] == "H"), 2)
            self._integrity.append(("target_trial_balance", cc, debit, credit, "debits and credits of the target company code computed in the target database"))
        self.metrics["target_aggregates"] = {"rfc_calls": client.calls, "packages": client.packages, "rows_avoided": sum(A["counts"].values()), "counts": A["counts"], "snapshot": client.snapshot, "journal_table": A["journal_table"], "ledger": A.get("ledger"), **({"assets_measure": A["assets_measure"], "assets_comparable": A.get("assets_comparable", True)} if A.get("assets_measure") else {})}

    def _rfc_readback(self, by_table: dict[str, int]) -> None:
        """Tables the APIs could not serve, read through the add-on on the target: company-code tables with the
        target company codes pushed down, MBEW by valuation area, loaded history tables by their loaded keys."""
        from ..runtime import rfc_config

        client = self.rfc
        rb: dict = {"transport": getattr(client.t, "name", "?"), "by_table": {}, "errors": {}}
        try:
            client.open_snapshot(sorted(self.unreadable & set(TABLES)))
        except RfcError as e:
            rb["errors"]["snapshot"] = str(e)
            self.metrics["rfc_readback"] = rb
            return
        cc_preds = [predicate("BUKRS", "EQ", cc) for cc in self.company_codes]
        aggregate_available = True

        def read(table: str, preds: list[dict], count_preds: list[dict] | None = None) -> bool:
            nonlocal aggregate_available
            try:
                rows = list(client.read_all(table, preds))
            except RfcError as e:
                rb["errors"][table] = str(e)
                return False
            n = self.add_rows(table, rows)
            rb["by_table"][table] = rb["by_table"].get(table, 0) + n
            self.unreadable.discard(table)
            self.read_via[table] = "rfc"
            td = TABLES.get(table)
            if td is not None:
                self._comparable[table] = set(td.fields) | set(td.key_fields)
            if aggregate_available and count_preds is not None:
                try:
                    expected = client.count(table, count_preds)
                    self._integrity.append(("target_read_integrity", table, expected, n, "row count computed in the target vs rows read through the add-on"))
                except RfcError as e:
                    if e.key in ("FU_NOT_FOUND", "NOT_AUTHORIZED"):
                        aggregate_available = False
                    else:
                        rb["errors"][f"{table}:count"] = str(e)
            return True

        for table in sorted(self.unreadable):
            td = TABLES.get(table)
            if td is None:
                continue
            if td.org_field and td.org_field.startswith("BUKRS") and table != "MBEW":
                read(table, cc_preds, cc_preds)
            elif table == "T001":
                read("T001", cc_preds, cc_preds)
        if "MBEW" in self.unreadable and self.valuation_areas:
            area_preds = [predicate("BWKEY", "EQ", a) for a in self.valuation_areas]
            self.metrics.setdefault("errors", {}).pop("MBEW", None)
            read("MBEW", area_preds, area_preds)
        for table, keys in sorted(self.loaded_keys.items()):
            if table not in self.unreadable or table not in TABLES or not keys:
                continue
            td = TABLES[table]
            if not td.key_fields:
                continue
            first = td.key_fields[0]
            values = sorted({k.split("|")[0] for k in keys})
            ok = True
            for i in range(0, len(values), rfc_config.key_chunk()):
                ok = read(table, [predicate(first, "EQ", v) for v in values[i : i + rfc_config.key_chunk()]]) and ok
                if not ok:
                    break
        rb.update({"snapshot": client.snapshot, "rfc_calls": client.calls, "packages": client.packages, "rows": sum(rb["by_table"].values()), "aggregate_available": aggregate_available})
        self.metrics["rfc_readback"] = rb
        for t in rb["by_table"]:
            by_table[t] = by_table.get(t, 0) + rb["by_table"][t]

    def _fetch(self, table: str, key: str) -> dict | None:
        if table not in _BOUND or (table, key) in self._missing:
            return None
        service, eb = _BOUND[table]
        try:
            ent, _etag = self.client.get(service, eb.entity_set, eb.key_props, key.split("|"))
        except ApiError as e:
            self.metrics.setdefault("errors", {})[f"{table}:{key}"] = str(e)
            ent = None
        if ent is None:
            self._missing.add((table, key))
            return None
        row = self._entity_rows(eb, [ent])[0]
        self.add_rows(table, [row])
        return self.by_key(table, key)

    def by_key(self, table: str, key: str) -> dict | None:
        row = super().by_key(table, key)
        if row is None and table in _BOUND:
            row = self._fetch(table, key)
        return row

    def get(self, table: str, **key_fields) -> dict | None:
        td = TABLES[table]
        return self.by_key(table, "|".join(str(key_fields.get(k, "")) for k in td.key_fields))


def build_target_view(session: Session, target: SapSystem, manifest, loaded_keys: dict[str, set[str]], source_view: RecordStore | None = None, force_api: bool = False, mode: str | None = None) -> ViewStore:
    """The target side of the reconciliation: the record store for simulated targets, the released APIs otherwise;
    with the add-on on the target, `mode` aggregate (or auto above SDTF_RECON_AGGREGATE_ABOVE journal lines)
    computes the totals in the target instead of reading the journal line items back."""
    mode = (mode or recon_mode()).lower()
    if mode not in MODES:
        raise ReconciliationViewError(f"mode must be one of {', '.join(MODES)}")
    if _uses_record_store(target) and not force_api:
        return record_store_view(session, target.id)
    if target.connector not in ("API", "SYNTHETIC"):  # SYNTHETIC only with force_api: the simulated gateway serves it
        raise ReconciliationViewError(f"no reconciliation read path for connector {target.connector}")
    defn = manifest.definition
    cc_map = defn.get("target_ownership", {}).get("company_code_map") or {}
    plant_map = defn.get("target_ownership", {}).get("plant_map") or {}
    target_ccs = {cc_map.get(c, c) for c in defn["company_codes"]}
    areas: set[str] = set()
    if source_view is not None:
        areas = {plant_map.get(str(r["BWKEY"]), str(r["BWKEY"])) for r in source_view.rows("T001K") if r["BUKRS"] in set(defn["company_codes"])}
    rfc_client = None
    aggregate = False
    decision = {"requested": mode, "reason": "requested"}
    if target_has_rfc(target):
        rfc_client = AbapAddonClient(make_transport(target.sid, target.meta, store_loader=lambda: RecordStore.load(session, target.id)))
        if mode == "aggregate":
            aggregate = True
        elif mode == "auto":
            try:
                rfc_client.open_snapshot(["BSEG"])
                n = rfc_client.count("BSEG", [predicate("BUKRS", "EQ", cc) for cc in sorted(target_ccs)])
                decision["bseg_rows"] = n
                aggregate = n > aggregate_above()
                decision["reason"] = f"BSEG {n} rows {'above' if aggregate else 'within'} SDTF_RECON_AGGREGATE_ABOVE={aggregate_above()}: {'aggregate' if aggregate else 'rows'}"
            except RfcError as e:
                if e.key not in ("FU_NOT_FOUND", "NOT_AUTHORIZED"):
                    raise
                decision["reason"] = "Z_SDTF_AGGREGATE unavailable on the target: rows"
    elif mode == "aggregate":
        raise ReconciliationViewError("aggregate-only reconciliation on the target needs the read-only add-on on the target (meta.rfc)")
    else:
        decision["reason"] = "no add-on on the target: rows through the APIs"
    view = ApiTargetView(session, target, target_ccs, loaded_keys, areas, (defn.get("fiscal_year_from"), defn.get("fiscal_year_to")), rfc_client=rfc_client, aggregate=aggregate)
    view.metrics["mode_decision"] = decision
    return view


def target_has_rfc(target: SapSystem) -> bool:
    """An API target that also hosts the read-only add-on (`meta.rfc` with a transport or destination): the
    reconciliation reads what the APIs cannot serve through it."""
    rfc = (target.meta or {}).get("rfc") or {}
    return bool(rfc.get("transport") or rfc.get("dest"))


def loaded_keys_of(backend, run_id: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for s in backend.iter_records(run_id, status="LOADED"):
        if s.target_key:
            out[s.table_name].add(s.target_key)
    return dict(out)


__all__ = ["SOURCE_TABLES", "MODES", "normalise", "ViewStore", "AggregateSourceView", "ApiTargetView", "ReconciliationViewError", "build_source_view", "build_target_view", "target_has_rfc", "journal_table_for", "journal_aggregates", "acdoca_from_journal", "asset_values", "asset_config", "record_store_view", "loaded_keys_of", "journal_rows", "eval_filter", "odata_filter", "record_key"]

"""Per-system read configuration of the reconciliation: which journal the aggregates come from on an S/4HANA
system, which ledger, how asset acquisition values and inventory values are read (`meta.rfc.journal_table`,
`meta.rfc.ledger`, `meta.rfc.assets`, `meta.rfc.inventory`). The API exposes it as one document so the Landscape
page can manage it without hand-editing the system's metadata; the transport and destination keys of `meta.rfc`
are never touched here, and secrets never pass through."""

from __future__ import annotations

import re

from ..models import SapSystem
from .views import ASSET_DEFAULTS, INVENTORY_DEFAULTS, LEADING_LEDGER, asset_config, inventory_config

KEYS = ("journal_table", "ledger", "assets", "inventory")

OPTIONS = {
    "journal_table": [
        {"value": "auto", "label": "auto: ACDOCA on S/4HANA when readable and holding rows, else BSEG"},
        {"value": "ACDOCA", "label": "ACDOCA (Universal Journal)"},
        {"value": "BSEG", "label": "BSEG with the open-item tables (classic)"},
    ],
    "assets_source": [
        {"value": "auto", "label": "auto: FAAV_ANLC, else APC line items (needs movement categories), else net postings"},
        {"value": "faav_anlc", "label": "compatibility view FAAV_ANLC (classic ANLC semantics)"},
        {"value": "apc_items", "label": "APC line items of ACDOCA / FAAT_DOC_IT by movement category"},
        {"value": "net", "label": "net asset postings of the Universal Journal (different measure, reported as WARN)"},
    ],
    "apc_tables": ["ACDOCA", "FAAT_DOC_IT"],
    "inventory_source": [
        {"value": "auto", "label": "auto: MBEW through the Material Ledger proxy view, else CKMLCR period totals, else inventory accounts"},
        {"value": "mbew", "label": "MBEW (SALK3 by valuation area; served by the proxy view MBV_MBEW on S/4HANA)"},
        {"value": "ml_period", "label": "Material Ledger period totals CKMLCR / CKMLHD (period and currency type below)"},
        {"value": "journal", "label": "balance of the inventory accounts in ACDOCA by plant (different measure, reported as WARN)"},
    ],
}

NOTES = [
    "Applies to the reconciliation's RFC reads of this system (source over the add-on, or target read-back and aggregate mode with meta.rfc). API-only targets ignore it.",
    "The asset and inventory chains only run on S/4HANA systems (product S4HANA); ECC-type systems keep ANLC and MBEW.",
    "No FAA_MOVCAT values are assumed: take the movement categories that carry acquisition and production costs from the domain on your system.",
    "The Material Ledger period defaults to the calendar month of the read; set it for non-calendar fiscal years or a closed cutover period.",
    "Verified on the simulated add-on only; the proxy-view behaviour of MBEW on A4H is a public-reference claim to confirm on the VM.",
]

_LEDGER = re.compile(r"^[A-Z0-9]{1,4}$")
_AREA = re.compile(r"^[0-9]{2}$")
_YEAR = re.compile(r"^[0-9]{4}$")
_POPER = re.compile(r"^[0-9]{1,3}$")
_CURTP = re.compile(r"^[0-9]{2}$")
_CODE = re.compile(r"^[A-Za-z0-9_]{1,10}$")


class ReadConfigError(ValueError):
    """The submitted configuration is not valid; the message says which field."""


def _codes(values, field: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in re.split(r"[,\s]+", values) if v]
    if not isinstance(values, list):
        raise ReadConfigError(f"{field} must be a list")
    out = []
    for v in values:
        v = str(v).strip()
        if not _CODE.match(v):
            raise ReadConfigError(f"{field}: {v!r} is not a valid code")
        if v not in out:
            out.append(v)
    return out


def validate_read_config(payload: dict) -> dict:
    """Normalise a submitted configuration to what is stored under meta.rfc: only the four read keys, only known
    fields, values checked. `auto` and defaults are stored explicitly so the stored document says what was chosen."""
    if not isinstance(payload, dict):
        raise ReadConfigError("the configuration must be an object")
    unknown = sorted(set(payload) - set(KEYS))
    if unknown:
        raise ReadConfigError(f"unknown keys: {', '.join(unknown)} (only {', '.join(KEYS)} are managed here)")
    out: dict = {}
    jt = str(payload.get("journal_table") or "auto").upper()
    if jt not in ("AUTO", "ACDOCA", "BSEG"):
        raise ReadConfigError("journal_table must be auto, ACDOCA or BSEG")
    out["journal_table"] = "" if jt == "AUTO" else jt
    ledger = str(payload.get("ledger") or LEADING_LEDGER).upper()
    if not _LEDGER.match(ledger):
        raise ReadConfigError("ledger must be 1 to 4 letters or digits (e.g. 0L)")
    out["ledger"] = ledger
    a = payload.get("assets") or {}
    if not isinstance(a, dict):
        raise ReadConfigError("assets must be an object")
    if set(a) - set(ASSET_DEFAULTS):
        raise ReadConfigError(f"assets: unknown fields {', '.join(sorted(set(a) - set(ASSET_DEFAULTS)))}")
    src = str(a.get("source") or "auto").lower()
    if src not in ("auto", "faav_anlc", "apc_items", "net"):
        raise ReadConfigError("assets.source must be auto, faav_anlc, apc_items or net")
    area = str(a.get("area") or ASSET_DEFAULTS["area"]).zfill(2)
    if not _AREA.match(area):
        raise ReadConfigError("assets.area must be a two-digit depreciation area (e.g. 01)")
    apc_tables = [str(t).upper() for t in (a.get("apc_tables") or ASSET_DEFAULTS["apc_tables"])]
    if not apc_tables or any(t not in OPTIONS["apc_tables"] for t in apc_tables):
        raise ReadConfigError("assets.apc_tables must be a non-empty subset of ACDOCA, FAAT_DOC_IT")
    out["assets"] = {"source": src, "area": area, "apc_movement_categories": _codes(a.get("apc_movement_categories"), "assets.apc_movement_categories"), "apc_tables": apc_tables}
    if src == "apc_items" and not out["assets"]["apc_movement_categories"]:
        raise ReadConfigError("assets.apc_movement_categories must name the FAA_MOVCAT values that carry APC when assets.source is apc_items")
    i = payload.get("inventory") or {}
    if not isinstance(i, dict):
        raise ReadConfigError("inventory must be an object")
    if set(i) - set(INVENTORY_DEFAULTS):
        raise ReadConfigError(f"inventory: unknown fields {', '.join(sorted(set(i) - set(INVENTORY_DEFAULTS)))}")
    isrc = str(i.get("source") or "auto").lower()
    if isrc not in ("auto", "mbew", "ml_period", "journal"):
        raise ReadConfigError("inventory.source must be auto, mbew, ml_period or journal")
    curtp = str(i.get("currency_type") or INVENTORY_DEFAULTS["currency_type"]).zfill(2)
    if not _CURTP.match(curtp):
        raise ReadConfigError("inventory.currency_type must be a two-digit Material Ledger currency type (e.g. 10)")
    period = i.get("period") or None
    if period is not None:
        if not isinstance(period, dict):
            raise ReadConfigError("inventory.period must be an object with year and poper, or empty for the calendar month of the read")
        year, poper = str(period.get("year") or "").strip(), str(period.get("poper") or "").strip()
        if not year and not poper:
            period = None
        else:
            if not _YEAR.match(year):
                raise ReadConfigError("inventory.period.year must be four digits")
            if not _POPER.match(poper) or not 1 <= int(poper) <= 16:
                raise ReadConfigError("inventory.period.poper must be a posting period between 1 and 16")
            period = {"year": year, "poper": poper.zfill(3)}
    accounts = _codes(i.get("inventory_accounts"), "inventory.inventory_accounts")
    if isrc == "journal" and not accounts:
        raise ReadConfigError("inventory.inventory_accounts must name the inventory accounts when inventory.source is journal")
    out["inventory"] = {"source": isrc, "currency_type": curtp, "period": period, "inventory_accounts": accounts}
    return out


def configured(system: SapSystem) -> dict:
    rfc = (system.meta or {}).get("rfc") or {}
    return {k: rfc[k] for k in KEYS if k in rfc}


def effective(system: SapSystem) -> dict:
    rfc = (system.meta or {}).get("rfc") or {}
    inv = inventory_config(system)
    jt = str(rfc.get("journal_table") or "").upper()
    return {
        "journal_table": jt if jt in ("ACDOCA", "BSEG") else "auto",
        "ledger": str(rfc.get("ledger") or LEADING_LEDGER),
        "assets": asset_config(system),
        "inventory": {**inv, "period_source": "configured" if isinstance(((rfc.get("inventory") or {}).get("period")), dict) and (rfc.get("inventory") or {}).get("period") else "calendar month of the read"},
    }


def describe(system: SapSystem) -> dict:
    """What the API returns: the stored subset, the effective values with defaults, whether it applies to this
    system and the options for a form."""
    has_rfc = bool(((system.meta or {}).get("rfc") or {}).get("transport"))
    is_s4 = (system.product or "").upper() == "S4HANA"
    return {"system_id": system.id, "applies": has_rfc, "s4hana": is_s4, "configured": configured(system), "effective": effective(system), "options": OPTIONS, "notes": NOTES, "defaults": {"journal_table": "auto", "ledger": LEADING_LEDGER, "assets": dict(ASSET_DEFAULTS), "inventory": dict(INVENTORY_DEFAULTS)}}


def apply(system: SapSystem, payload: dict) -> dict:
    """Validate and store the configuration under meta.rfc, leaving every other key of meta untouched; an empty
    payload resets to the defaults (the keys are removed)."""
    meta = dict(system.meta or {})
    rfc = dict(meta.get("rfc") or {})
    if not payload:
        for k in KEYS:
            rfc.pop(k, None)
    else:
        cfg = validate_read_config(payload)
        for k in KEYS:
            if k == "journal_table" and not cfg[k]:
                rfc.pop(k, None)
            else:
                rfc[k] = cfg[k]
    meta["rfc"] = rfc
    system.meta = meta  # new dict: the JSON column notices the change
    return describe(system)

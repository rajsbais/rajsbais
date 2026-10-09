"""Sensitive-data discovery and masking.

Honest terminology (enforced in `protection_class`):
  * PSEUDONYMIZE  - deterministic keyed (HMAC) substitution. Stable across refreshes. Re-identification is
                    possible for whoever holds the key plus candidate values => *pseudonymization*, never labelled anonymization.
  * ANONYMIZE     - same substitution but with a per-run random key that is destroyed after the run. Consistent
                    inside one run (preserves joins), irreversible afterwards. Quasi-identifiers that are NOT masked
                    (city, postal code, dates, amounts) can still enable inference; that residual risk is reported.
  * TOKENIZE      - pseudonym + encrypted vault entry; reversible only through `TokenVault.reidentify`, which
                    requires an explicit privilege and is audited by the caller.

The substitution functions are format-preserving by construction (character-class and length preserving, valid
IBAN check digits) but are NOT NIST FF1/FF3 encryption. A production deployment should use a vetted FPE library/HSM.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from enum import Enum

from cryptography.fernet import Fernet

from ..sap.ddic import TABLES

_FIRST = ["Alder", "Birch", "Cedar", "Dune", "Ember", "Fjord", "Grove", "Harbor", "Iris", "Juniper"]
_LAST = ["Northwind", "Contoso", "Fabrikam", "Tailspin", "Litware", "Woodgrove", "Adatum", "Proseware", "Lucerne", "Margie"]
_CORP = ["Industries", "Logistics", "Retail", "Components", "Trading", "Foods", "Systems"]
_STREET = ["Maple", "Oak", "Elm", "Lake", "Hill", "Park", "River"]


class MaskMode(str, Enum):
    PSEUDONYMIZE = "PSEUDONYMIZE"
    ANONYMIZE = "ANONYMIZE"
    TOKENIZE = "TOKENIZE"


PROTECTION_CLASS = {
    MaskMode.PSEUDONYMIZE: "pseudonymized",
    MaskMode.ANONYMIZE: "anonymized-best-effort",
    MaskMode.TOKENIZE: "pseudonymized-reversible",
}


class Prng:
    def __init__(self, seed: bytes):
        self.seed, self.ctr = seed, 0

    def below(self, n: int) -> int:
        self.ctr += 1
        d = hashlib.sha256(self.seed + self.ctr.to_bytes(4, "big")).digest()
        return int.from_bytes(d[:8], "big") % n


def _fp(value: str, p: Prng, keep_prefix: int = 0) -> str:
    out = []
    for i, ch in enumerate(value):
        if i < keep_prefix:
            out.append(ch)
        elif ch.isdigit():
            out.append(str(p.below(10)))
        elif ch.isalpha():
            base = "A" if ch.isupper() else "a"
            out.append(chr(ord(base) + p.below(26)))
        else:
            out.append(ch)
    return "".join(out)


def _iban(value: str, p: Prng) -> str:
    v = value.replace(" ", "")
    if len(v) < 8 or not v[:2].isalpha():
        return _fp(value, p)
    cc = v[:2].upper()
    bban = "".join(str(p.below(10)) for _ in range(len(v) - 4))
    num = "".join(str(int(c, 36)) for c in bban + cc) + "00"
    return f"{cc}{98 - int(num) % 97:02d}{bban}"


def _shift_date(v: str, p: Prng) -> str:
    """Shift an ISO date by a keyed offset of 30 to 3000 days (forward or back): the age profile stays plausible, the date itself never survives."""
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(v[:10])
    except ValueError:
        return f"{1950 + p.below(56)}-{1 + p.below(12):02d}-{1 + p.below(28):02d}"
    off = 30 + p.below(2971)
    return (d + timedelta(days=off if p.below(2) else -off)).isoformat()


STRATEGIES = {
    "NAME": lambda v, p: f"{_FIRST[p.below(10)]} {_LAST[p.below(10)]} {_CORP[p.below(7)]}",
    "STREET": lambda v, p: f"{p.below(98) + 1} {_STREET[p.below(7)]} Street",
    "PHONE": lambda v, p: _fp(v, p, keep_prefix=0),
    "EMAIL": lambda v, p: f"user{p.below(16**6):06x}@example.test",
    "IBAN": _iban,
    "BANK_ACCOUNT": lambda v, p: _fp(v, p),
    "TAX_ID": lambda v, p: _fp(v, p, keep_prefix=2 if v[:2].isalpha() else 0),
    "REDACT": lambda v, p: "X" * min(max(len(v), 4), 12),
    "BIRTHDATE": lambda v, p: _shift_date(v, p),
    "USER_ID": lambda v, p: "U" + "".join(str(p.below(10)) for _ in range(max(len(v) - 1, 4))),
    "FIRST_NAME": lambda v, p: _FIRST[p.below(10)],
    "LAST_NAME": lambda v, p: _LAST[p.below(10)],
    "AMOUNT": lambda v, p: _perturb(v, p),
}
NUMERIC_STRATEGIES = {"AMOUNT"}  # applied to numbers, not only to text


def _perturb(v: str, p: Prng) -> float:
    """Scale an amount by a keyed factor in [0.60, 0.97] or [1.03, 1.40]: never the original, never exactly proportional across people."""
    try:
        x = float(v)
    except ValueError:
        return 0.0
    f = 0.60 + p.below(3700) / 10000
    return round(x * (f if f < 0.97 else f + 0.06), 2)

# (table, field) -> (category, strategy). Seed catalog for the SAP subset; extend per customer/Z-fields.
CATALOG: dict[tuple[str, str], tuple[str, str]] = {
    ("KNA1", "NAME1"): ("name", "NAME"), ("KNA1", "STRAS"): ("street", "STREET"),
    ("KNA1", "TELF1"): ("phone", "PHONE"), ("KNA1", "STCD1"): ("tax_id", "TAX_ID"),
    ("LFA1", "NAME1"): ("name", "NAME"), ("LFA1", "TELF1"): ("phone", "PHONE"), ("LFA1", "STCD1"): ("tax_id", "TAX_ID"),
    ("ADRC", "NAME1"): ("name", "NAME"), ("ADRC", "STREET"): ("street", "STREET"),
    ("ADRC", "TEL_NUMBER"): ("phone", "PHONE"), ("ADRC", "SMTP_ADDR"): ("email", "EMAIL"),
    ("BUT000", "NAME_ORG1"): ("name", "NAME"), ("BUT000", "BU_SORT1"): ("name", "NAME"),
    ("KNBK", "BANKN"): ("bank_account", "BANK_ACCOUNT"), ("KNBK", "IBAN"): ("iban", "IBAN"), ("KNBK", "KOINH"): ("name", "NAME"),
    ("LFBK", "BANKN"): ("bank_account", "BANK_ACCOUNT"), ("LFBK", "IBAN"): ("iban", "IBAN"), ("LFBK", "KOINH"): ("name", "NAME"),
    # flight demo model (SCUSTOM / SBOOK): customers and passengers are people
    ("SCUSTOM", "NAME"): ("name", "NAME"), ("SCUSTOM", "STREET"): ("street", "STREET"), ("SCUSTOM", "POSTBOX"): ("identifier", "REDACT"),
    ("SCUSTOM", "TELEPHONE"): ("phone", "PHONE"), ("SCUSTOM", "EMAIL"): ("email", "EMAIL"), ("SCUSTOM", "WEBUSER"): ("identifier", "REDACT"),
    ("SBOOK", "PASSNAME"): ("name", "NAME"), ("SBOOK", "PASSBIRTH"): ("birthdate", "BIRTHDATE"),
    # QM: the people who inspected and decided
    ("QASR", "PRUEFER"): ("user", "USER_ID"), ("QAVE", "VAENAME"): ("user", "USER_ID"),
    # PM: the people who reported a malfunction
    ("QMEL", "ERNAM"): ("user", "USER_ID"),
    # HR infotypes: special-category data. Pay is perturbed, identifiers and bank details are replaced, dates of birth shifted.
    ("PA0002", "NACHN"): ("name", "LAST_NAME"), ("PA0002", "VORNA"): ("name", "FIRST_NAME"), ("PA0002", "GBDAT"): ("birthdate", "BIRTHDATE"),
    ("PA0002", "PERID"): ("national_id", "TAX_ID"), ("PA0006", "STRAS"): ("street", "STREET"), ("PA0006", "TELNR"): ("phone", "PHONE"),
    ("PA0008", "BET01"): ("pay", "AMOUNT"), ("PA0008", "ANSAL"): ("pay", "AMOUNT"),
    ("PA0009", "EMFTX"): ("name", "NAME"), ("PA0009", "BANKN"): ("bank_account", "BANK_ACCOUNT"), ("PA0009", "BANKL"): ("bank_account", "BANK_ACCOUNT"),
}
HR_TABLES = {"PA0001", "PA0002", "PA0003", "PA0006", "PA0008", "PA0009"}  # special-category personal data: per-run anonymization only
CATEGORY_LABEL = {"name": "Names", "street": "Addresses", "phone": "Telephone numbers", "email": "Email addresses",
                  "iban": "Bank details", "bank_account": "Bank details", "tax_id": "Tax identifiers", "identifier": "Account and mailbox identifiers",
                  "birthdate": "Dates of birth", "national_id": "National identifiers", "pay": "Pay and salary", "user": "User ids of employees"}

_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
_PHONE = re.compile(r"^(\+|\()[\d\s\-().]{7,}$")
_PATTERNS = [("email", "EMAIL", _EMAIL), ("iban", "IBAN", _IBAN), ("phone", "PHONE", _PHONE)]


@dataclass
class Rule:
    table: str
    field: str
    strategy: str
    category: str
    mode: MaskMode = MaskMode.PSEUDONYMIZE

    def to_dict(self):
        return {"table": self.table, "field": self.field, "strategy": self.strategy, "category": self.category,
                "mode": self.mode.value, "protection_class": PROTECTION_CLASS[self.mode]}


@dataclass
class MaskingPolicy:
    id: str
    name: str
    rules: list[Rule] = field(default_factory=list)
    description: str = ""

    def validate(self) -> list[str]:
        errs = []
        for r in self.rules:
            td = TABLES.get(r.table)
            if td is None:
                errs.append(f"{r.table}.{r.field}: unknown table")
            elif r.field not in td.fields:
                errs.append(f"{r.table}.{r.field}: unknown field")
            elif r.field in td.keys:
                errs.append(f"{r.table}.{r.field}: key fields cannot be masked (would break referential integrity)")
            if r.strategy not in STRATEGIES:
                errs.append(f"{r.table}.{r.field}: unknown strategy {r.strategy}")
        return errs

    def fields(self) -> set[tuple[str, str]]:
        return {(r.table, r.field) for r in self.rules}

    def to_dict(self):
        return {"id": self.id, "name": self.name, "description": self.description,
                "rules": [r.to_dict() for r in self.rules], "errors": self.validate()}


def template(policy_id: str) -> MaskingPolicy:
    if policy_id == "gdpr-standard":
        mode, name, desc = MaskMode.PSEUDONYMIZE, "GDPR standard (pseudonymization)", \
            "Deterministic keyed pseudonyms, stable across refreshes. Not anonymization."
    elif policy_id == "gdpr-strict":
        mode, name, desc = MaskMode.ANONYMIZE, "GDPR strict (per-run anonymization)", \
            "Per-run random key destroyed after the run; irreversible, not stable across runs."
    else:
        raise KeyError(policy_id)
    return MaskingPolicy(policy_id, name, [Rule(t, f, s, c, mode) for (t, f), (c, s) in CATALOG.items()], desc)


TEMPLATES = ["gdpr-standard", "gdpr-strict"]


def discover_sensitive(rows_by_table: dict[str, list[dict]], pattern_threshold: float = 0.6) -> list[dict]:
    """Catalog + value-pattern discovery over the rows that are about to be copied."""
    found = []
    for table, rows in rows_by_table.items():
        td = TABLES.get(table)
        if not td or td.config or not rows:
            continue
        for f in td.fields:
            if f in td.keys:
                continue
            if (table, f) in CATALOG:
                cat, strat = CATALOG[(table, f)]
                found.append({"table": table, "field": f, "category": cat, "strategy": strat,
                              "confidence": 1.0, "source": "catalog", "samples": len(rows)})
                continue
            vals = [str(r.get(f)) for r in rows if isinstance(r.get(f), str) and r.get(f)]
            if len(vals) < 1:
                continue
            for cat, strat, rx in _PATTERNS:
                hit = sum(1 for v in vals if rx.match(v))
                if hit / len(vals) >= pattern_threshold:
                    found.append({"table": table, "field": f, "category": cat, "strategy": strat,
                                  "confidence": round(hit / len(vals), 2), "source": "pattern", "samples": len(vals)})
                    break
    return found


class TokenVault:
    """Reversible token store. Values are Fernet-encrypted at rest; reidentify needs an explicit privilege."""

    def __init__(self, key: bytes | None = None):
        self._f = Fernet(key or Fernet.generate_key())
        self._store: dict[str, bytes] = {}

    def put(self, token: str, original: str) -> None:
        self._store[token] = self._f.encrypt(original.encode())

    def reidentify(self, token: str, *, privileged: bool) -> str:
        if not privileged:
            raise PermissionError("re-identification requires the privacy_officer privilege")
        return self._f.decrypt(self._store[token]).decode()


class MaskingEngine:
    def __init__(self, policy: MaskingPolicy, persistent_key: bytes | None = None, vault: TokenVault | None = None):
        errs = policy.validate()
        if errs:
            raise ValueError("invalid masking policy: " + "; ".join(errs))
        self.policy = policy
        self._by_field = {(r.table, r.field): r for r in policy.rules}
        self._persistent = persistent_key or os.environ.get("RF_MASKING_KEY", "").encode() or os.urandom(32)
        self._ephemeral: bytes | None = os.urandom(32)
        self.vault = vault or TokenVault()
        self.stats: dict[tuple[str, str], dict[str, int]] = {}

    def _key(self, mode: MaskMode) -> bytes:
        if mode == MaskMode.ANONYMIZE:
            if self._ephemeral is None:
                raise RuntimeError("ephemeral anonymization key already destroyed")
            return self._ephemeral
        return self._persistent

    def mask_value(self, rule: Rule, value: str) -> str:
        seed = hmac.new(self._key(rule.mode), f"{rule.category}|{value}".encode(), hashlib.sha256).digest()
        out = STRATEGIES[rule.strategy](value, Prng(seed))
        if rule.mode == MaskMode.TOKENIZE:
            self.vault.put(str(out), value)
        return out

    def mask_row(self, table: str, row: dict) -> dict:
        out = dict(row)
        for (t, f), rule in self._by_field.items():
            if t != table or f not in row:
                continue
            st = self.stats.setdefault((t, f), {"rows": 0, "masked": 0, "empty": 0})
            st["rows"] += 1
            v = row[f]
            if rule.strategy in NUMERIC_STRATEGIES and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[f] = self.mask_value(rule, repr(v))
                st["masked"] += 1
                continue
            if not isinstance(v, str) or v == "":
                st["empty"] += 1
                continue
            out[f] = self.mask_value(rule, v)
            st["masked"] += 1
        return out

    def destroy_ephemeral_key(self) -> None:
        self._ephemeral = None

    def coverage(self, required: list[dict]) -> dict:
        have = self.policy.fields()
        missing = [r for r in required if (r["table"], r["field"]) not in have]
        return {"required": len(required), "covered": len(required) - len(missing), "missing": missing,
                "complete": not missing}

    def report(self) -> dict:
        modes = {r.mode for r in self.policy.rules}
        return {
            "policy": self.policy.id,
            "protection_classes": sorted(PROTECTION_CLASS[m] for m in modes),
            "reversible": any(m == MaskMode.TOKENIZE for m in modes),
            "keyed_pseudonyms_reidentifiable_with_key": any(m != MaskMode.ANONYMIZE for m in modes),
            "residual_risk_note": "Unmasked quasi-identifiers (city, postal code, dates, amounts, document structure) "
                                  "may still permit inference; masking is not a legal anonymization determination.",
            "fields": [{"table": t, "field": f, **s, "mode": self._by_field[(t, f)].mode.value,
                        "strategy": self._by_field[(t, f)].strategy} for (t, f), s in sorted(self.stats.items())],
        }

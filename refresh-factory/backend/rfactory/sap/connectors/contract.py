"""Contract checks any read-side SAP adapter must satisfy, evaluated against a REFERENCE adapter holding the same data.

The simulator is the reference: a connector that passes returns what the simulator returns for the same questions (modulo number
types), so the rest of the platform behaves identically over it. Passing says nothing about a real system's data volume or quirks.
"""
from __future__ import annotations

from ..ddic import TABLES, key_of


def norm(row: dict) -> str:
    out = {}
    for k, v in sorted(row.items()):
        out[k] = round(float(v), 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    return repr(out)


def normset(rows) -> list[str]:
    return sorted(norm(r) for r in rows)


def source_contract(adapter, reference, tables=None, sample: int = 3) -> list[str]:
    """Returns the list of contract violations (empty = conforms)."""
    bad: list[str] = []
    for t in tables or [t for t, d in TABLES.items() if not d.config or t in ("T001", "T001W")]:
        ref = reference.select(t)
        got = adapter.select(t)
        if normset(got) != normset(ref):
            bad.append(f"{t}: select() differs ({len(got)} rows vs {len(ref)})")
            continue
        if len({key_of(t, r) for r in got}) != len(got):
            bad.append(f"{t}: duplicate keys")
        if adapter.count(t) != len(ref):
            bad.append(f"{t}: count() {adapter.count(t)} != {len(ref)}")
        for r in ref[:sample]:
            k = key_of(t, r)
            g = adapter.get(t, k)
            if g is None or norm(g) != norm(r):
                bad.append(f"{t}: get({k}) differs")
        if adapter.get(t, tuple("~absent~" for _ in TABLES[t].keys)) is not None:
            bad.append(f"{t}: get() of an absent key returned a row")
        for f in TABLES[t].fields[:3]:
            vals = [r[f] for r in ref[:2] if r.get(f) not in (None, "")]
            for v in vals:
                if normset(adapter.lookup(t, f, v)) != normset(reference.lookup(t, f, v)):
                    bad.append(f"{t}: lookup({f}={v!r}) differs")
            if vals and hasattr(adapter, "select_in") and normset(adapter.select_in(t, f, vals)) != normset(reference.select_in(t, f, vals)):
                bad.append(f"{t}: select_in({f}) differs")
    for t, f in (("VBAK", "ERDAT"), ("AUFK", "ERDAT")):
        if t in TABLES and (tables is None or t in tables) and hasattr(adapter, "select_between"):
            lo, hi = "2026-07-01", "2026-09-01"
            if normset(adapter.select_between(t, f, lo, hi)) != normset(reference.select_between(t, f, lo, hi)):
                bad.append(f"{t}: select_between({f}) differs")
    if adapter.reference_date() is None:
        bad.append("reference_date() is None")
    for forbidden in ("upsert", "delete", "set_number_level", "sim_insert"):
        if hasattr(adapter, forbidden):
            bad.append(f"a read-only source adapter exposes {forbidden}()")
    return bad

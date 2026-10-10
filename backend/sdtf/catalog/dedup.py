"""Master-data deduplication across source systems (mergers / consolidations).

Candidates are found by normalised business keys (name + country for partners, description for materials). Each
candidate names a survivor (the record of the first source in merge order) and the duplicates that must be mapped
onto it. Output feeds the rule factory (`dedup` parameter) as lookups: duplicate source key -> survivor target key.
"""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy.orm import Session

from .store import RecordStore

KINDS = {
    "customers": ("KNA1", "KUNNR", lambda r: f"{_norm(r.get('NAME1'))}|{r.get('LAND1', '')}"),
    "vendors": ("LFA1", "LIFNR", lambda r: f"{_norm(r.get('NAME1'))}|{r.get('LAND1', '')}"),
    "materials": ("MARA", "MATNR", lambda r: _norm(r.get("MAKTX"))),
}


def _norm(v) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()


def find_duplicate_masters(session: Session, source_system_ids: list[str], kinds: tuple[str, ...] = ("customers", "vendors", "materials")) -> dict:
    """Returns {kind: [{"match_key", "survivor": {system, key}, "duplicates": [{system, key}], "confidence"}]}."""
    stores = [(sid, RecordStore.load(session, sid, tables=[KINDS[k][0] for k in kinds])) for sid in source_system_ids]
    out: dict[str, list[dict]] = {}
    for kind in kinds:
        table, key_field, key_fn = KINDS[kind]
        groups: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
        for sid, store in stores:
            for r in store.rows(table):
                mk = key_fn(r)
                if mk and mk != "|":
                    groups[mk].append((sid, r[key_field], r))
        cands = []
        for mk, members in groups.items():
            systems = {m[0] for m in members}
            if len(members) < 2:
                continue
            survivor = members[0]
            dups = [m for m in members[1:] if m[1] != survivor[1] or m[0] != survivor[0]]
            if not dups:
                continue
            exact = all(m[2].get("LAND1") == survivor[2].get("LAND1") for m in dups) if kind != "materials" else all(m[2].get("MTART") == survivor[2].get("MTART") for m in dups)
            cands.append({"match_key": mk, "survivor": {"system": survivor[0], "key": survivor[1]}, "duplicates": [{"system": m[0], "key": m[1]} for m in dups], "cross_system": len(systems) > 1, "confidence": 0.9 if exact else 0.7})
        out[kind] = sorted(cands, key=lambda c: c["match_key"])
    return out


def dedup_lookups_for_source(candidates: dict, source_system_id: str, survivor_target_key) -> dict:
    """Lookups for one (non-leading) source: its duplicate keys -> the survivor's *target* key. survivor_target_key(kind, key)
    returns the key under which the survivor was loaded (e.g. after BP prefixing)."""
    lookups: dict[str, dict[str, str]] = {}
    for kind, cands in candidates.items():
        m = {}
        for c in cands:
            for d in c["duplicates"]:
                if d["system"] == source_system_id and c["survivor"]["system"] != source_system_id:
                    m[d["key"]] = survivor_target_key(kind, c["survivor"]["key"])
        if m:
            lookups[kind] = m
    return lookups

"""Project registry of migration objects (imported from the target) and the per-project lookup."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.migration_objects import CATALOGUE_SOURCE, lookup, lookup_table, normalize_release
from ..catalog.tables import TABLES
from ..models import MigrationObjectEntry, SapSystem


def entry_out(e: MigrationObjectEntry) -> dict:
    return {"id": e.id, "project_id": e.project_id, "release": e.release, "name": e.name, "object_id": e.object_id, "object_types": list(e.object_types or []), "tables": list(e.tables or []), "notes": e.notes, "source": e.source, "imported_by": e.imported_by, "created_at": e.created_at}


def project_registry(session: Session, project_id: str) -> list[dict]:
    """Registry entries in the shape `lookup()` consumes."""
    rows = session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == project_id).order_by(MigrationObjectEntry.release, MigrationObjectEntry.name)).scalars().all()
    return [{"release": e.release, "name": e.name, "id": e.object_id, "object_types": list(e.object_types or []), "tables": list(e.tables or []), "notes": e.notes, "source": e.source or "project registry", "entry_id": e.id} for e in rows]


def target_release(session: Session, project_id: str) -> str:
    tgt = session.execute(select(SapSystem).where(SapSystem.project_id == project_id, SapSystem.role == "TARGET")).scalars().first()
    return tgt.release if tgt else ""


def import_entries(session: Session, project_id: str, entries: list[dict], actor: str, release: str | None = None, replace: bool = False, source: str = "") -> list[MigrationObjectEntry]:
    """Import entries `{name, id?, release?, object_types?, tables?, notes?}`; `release` is the default for entries
    without one ("*" = any). Names are matched case-insensitively per release and replaced in place; `replace`
    drops the project's other entries first. Business object and table names are validated."""
    rel_default = normalize_release(release) or ("*" if release in (None, "", "*") else "")
    if release and rel_default == "":
        raise ValueError(f"unrecognised release {release!r} (expected an S/4HANA release such as 2023 or 'cloud')")
    existing = {(e.release, e.name.lower()): e for e in session.execute(select(MigrationObjectEntry).where(MigrationObjectEntry.project_id == project_id)).scalars().all()}
    if replace:
        for e in list(existing.values()):
            session.delete(e)
        existing = {}
        session.flush()
    out = []
    for raw in entries:
        name = str(raw.get("name", "")).strip()
        if not name:
            raise ValueError("every entry needs a name")
        rel = raw.get("release")
        rel_n = "*" if rel == "*" else (rel_default if rel in (None, "") else (normalize_release(rel) or ""))
        if rel not in (None, "", "*") and not rel_n:
            raise ValueError(f"unrecognised release {rel!r} for {name}")
        ots = [str(x) for x in raw.get("object_types") or []]
        bad = [x for x in ots if x not in BUSINESS_OBJECTS]
        if bad:
            raise ValueError(f"unknown business object(s) {', '.join(bad)} for {name}")
        tabs = [str(x).upper() for x in raw.get("tables") or []]
        bad = [x for x in tabs if x not in TABLES]
        if bad:
            raise ValueError(f"unknown table(s) {', '.join(bad)} for {name}")
        e = existing.get((rel_n, name.lower()))
        if e is None:
            e = MigrationObjectEntry(project_id=project_id, release=rel_n, name=name)
            session.add(e)
            existing[(rel_n, name.lower())] = e
        e.object_id, e.object_types, e.tables = str(raw.get("id") or raw.get("object_id") or "").strip(), ots, tabs
        e.notes, e.source, e.imported_by = str(raw.get("notes") or "")[:400], (source or str(raw.get("source") or "imported from the target's object list"))[:200], actor
        out.append(e)
    session.flush()
    record_event(session, actor, "MIGRATION_OBJECTS_IMPORTED", "PROJECT", project_id, {"entries": len(out), "release": rel_default, "replace": replace})
    return out


def project_lookup(session: Session, project_id: str, release: str | None = None, object_type: str = "") -> dict:
    """The resolution table for a project: its registry first, then the catalogue, for the target's release."""
    rel = release or target_release(session, project_id)
    reg = project_registry(session, project_id)
    return {"release": rel, "normalized_release": normalize_release(rel), "registry_entries": len(reg), "catalogue_source": CATALOGUE_SOURCE, "objects": lookup_table(object_type, rel, reg)}


def resolve_for_export(session: Session, project_id: str, release: str, object_type: str, tables: list[str]) -> dict:
    """Lookup for one exported object: overall result plus per-table results when the tables decide."""
    reg = project_registry(session, project_id)
    overall = lookup(object_type, release, tables[0] if len(tables) == 1 else None, reg)
    by_table = {t: lookup(object_type, release, t, reg) for t in tables}
    names = {r["name"] for r in by_table.values() if r["status"] == "found"}
    if overall["status"] == "candidates" and names:
        overall = {**overall, "note": "the staging tables map to " + ", ".join(sorted(names)) + "; see by_table"}
    return {**{k: v for k, v in overall.items() if k != "candidates"}, "candidates": [c["name"] for c in overall.get("candidates", [])], "by_table": {t: {"status": r["status"], "name": r["name"], "id": r["id"], "confidence": r["confidence"]} for t, r in by_table.items()}}

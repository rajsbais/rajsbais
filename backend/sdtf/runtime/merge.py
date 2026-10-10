"""Multi-source merger / consolidation orchestration (scenario A4).

A merge group executes one approved manifest per source system into one shared target, in order. Before any run:
* cross-system key collisions are computed by applying each source's ruleset to the header keys of all transferred
  objects (two sources producing the same target key with different content = collision);
* master-data duplicates are surfaced (catalog/dedup.py) so the rule factory can redirect references.
Financial reconciliation is deferred to the group (reconciliation/service.py::reconcile_merge_group) because the
target company code holds the union of all sources.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.dedup import KINDS, dedup_lookups_for_source, find_duplicate_masters
from ..catalog.store import RecordStore
from ..models import MigrationRun, RuleSet, SapSystem, ScopeManifest
from ..reconciliation.service import reconcile_merge_group
from ..rules.engine import RuleError, SkipRecord, parse_ruleset, target_key, transform_record
from .extraction import TRANSFER_CLASSES, build_extractor
from .pipeline import RunPrecondition, start_run


def _members(session: Session, project_id: str, sources: list[dict]) -> list[tuple[ScopeManifest, RuleSet, SapSystem]]:
    out = []
    targets = set()
    for s in sources:
        m = session.get(ScopeManifest, s["manifest_id"])
        rs = session.get(RuleSet, s["ruleset_id"])
        if m is None or rs is None or m.project_id != project_id or rs.project_id != project_id:
            raise RunPrecondition("manifest or ruleset not found in project")
        if m.status != "APPROVED" or rs.status != "APPROVED":
            raise RunPrecondition(f"manifest {m.name} v{m.version} / ruleset {rs.name} must be APPROVED")
        src = session.get(SapSystem, m.definition["source_system_id"])
        targets.add(m.definition["target_system_id"])
        out.append((m, rs, src))
    if len(targets) != 1:
        raise RunPrecondition("all manifests of a merge group must share one target system")
    if len({src.id for _, _, src in out}) != len(out):
        raise RunPrecondition("each source system may appear once in a merge group")
    return out


def plan_merge(session: Session, project_id: str, sources: list[dict]) -> dict:
    members = _members(session, project_id, sources)
    seen: dict[tuple[str, str], tuple[str, dict]] = {}
    collisions: list[dict] = []
    per_source = []
    for m, rs_row, src in members:
        rs = parse_ruleset(rs_row.source_yaml)
        cls = m.selection.get("classification", {})
        tables = sorted({BUSINESS_OBJECTS[c["type"]].header_table for c in cls.values() if c["classification"] in TRANSFER_CLASSES and c["type"] in BUSINESS_OBJECTS})
        tables += sorted({t for c in cls.values() if c["classification"] in TRANSFER_CLASSES and c["type"] in BUSINESS_OBJECTS for t in BUSINESS_OBJECTS[c["type"]].item_tables} | {"T001K"})
        store = RecordStore.load(session, src.id, tables=sorted(set(tables)))
        extractor = build_extractor(session, src, cls, set(m.definition["company_codes"]), store=store)
        n_keys, n_skipped, n_rejected = 0, 0, 0
        sources = {p.id: extractor._source_for(p) for p in extractor.partitions()}
        node_part = {nid: pid for pid, nodes in extractor.plan().objects_by_partition.items() for nid in nodes}
        for nid, c in cls.items():
            if c["classification"] not in TRANSFER_CLASSES or c["type"] not in BUSINESS_OBJECTS:
                continue
            bo = BUSINESS_OBJECTS[c["type"]]
            if nid not in node_part:
                continue
            for table, row in extractor._rows_for_object(sources[node_part[nid]], bo.id, nid.split(":", 1)[1]):
                try:
                    out, _ = transform_record(rs, table, row)
                except SkipRecord:
                    n_skipped += 1
                    continue  # the skipped record is not loaded; dependent views still are
                except RuleError:
                    n_rejected += 1
                    continue
                tk = target_key(table, out)
                n_keys += 1
                prev = seen.get((table, tk))
                if prev is not None and prev[0] != src.id:
                    if prev[1] != out:
                        collisions.append({"table": table, "target_key": tk, "sources": [prev[0], src.id], "object_type": c["type"]})
                else:
                    seen[(table, tk)] = (src.id, out)
        per_source.append({"manifest_id": m.id, "ruleset_id": rs_row.id, "source_system_id": src.id, "sid": src.sid, "company_codes": m.definition["company_codes"], "target_company_codes": sorted({(m.definition.get("target_ownership", {}).get("company_code_map") or {}).get(cc, cc) for cc in m.definition["company_codes"]}), "objects": n_keys, "skipped_by_rules": n_skipped, "rejected_by_rules": n_rejected})
    by_table = defaultdict(int)
    for c in collisions:
        by_table[c["table"]] += 1
    target_ccs = sorted({tcc for p in per_source for tcc in p["target_company_codes"]})
    merged_ccs = [tcc for tcc in target_ccs if sum(1 for p in per_source if tcc in p["target_company_codes"]) > 1]
    return {
        "target_system_id": members[0][0].definition["target_system_id"],
        "sources": per_source,
        "target_company_codes": target_ccs,
        "company_codes_merged_from_several_sources": merged_ccs,
        "collisions": {"count": len(collisions), "by_table": dict(by_table), "samples": collisions[:50]},
        "ready": not collisions,
        "recommendation": "Assign disjoint number-range offsets / key prefixes per source (rule factory source_index) and resolve master-data duplicates before running" if collisions else "No cross-system key collisions detected",
    }


def start_merge_run(session: Session, project_id: str, sources: list[dict], actor: str) -> dict:
    plan = plan_merge(session, project_id, sources)
    if not plan["ready"]:
        raise RunPrecondition(f"{plan['collisions']['count']} cross-system key collision(s); resolve them before running the merge")
    group = uuid.uuid4().hex
    record_event(session, actor, "MERGE_STARTED", "MERGE_GROUP", group, {"sources": [s["source_system_id"] for s in plan["sources"]], "target": plan["target_system_id"]})
    runs: list[MigrationRun] = []
    for s in plan["sources"]:
        runs.append(start_run(session, project_id, s["manifest_id"], s["ruleset_id"], actor, merge_group=group))
    target = RecordStore.load(session, plan["target_system_id"])
    fin = reconcile_merge_group(session, runs, target)
    overall = fin["overall"]
    for r in runs:
        layer = (r.report or {}).get("reconciliation", {}).get("overall", "PASS")
        if layer == "FAIL" or (layer == "WARN" and overall != "FAIL"):
            overall = layer if layer == "FAIL" else overall if overall == "FAIL" else "WARN"
        r.metrics = {**r.metrics, "merge_group": group, "merge_financial": fin}
        r.report = {**(r.report or {}), "merge_group": {"id": group, "runs": [x.id for x in runs], "financial": fin}}
    record_event(session, actor, "MERGE_COMPLETED", "MERGE_GROUP", group, {"runs": [r.id for r in runs], "financial": fin["overall"]})
    session.flush()
    return {"merge_group": group, "plan": plan, "runs": [{"id": r.id, "source_system_id": r.source_system_id, "status": r.status, "reconciliation": (r.report or {}).get("reconciliation", {}).get("overall")} for r in runs], "financial": fin, "overall": overall}


_KIND_TYPE = {"customers": "MD.Customer", "vendors": "MD.Vendor", "materials": "MD.Material"}


def dedup_for_merge(session: Session, project_id: str, leading: dict, source_system_id: str) -> dict:
    """Dedup lookups for a non-leading source: duplicate key -> survivor key *as loaded by the leading source*.
    Survivors are limited to master records the leading manifest actually loads (transferred or reference stub),
    and their target keys are derived by applying the leading ruleset, not guessed."""
    m = session.get(ScopeManifest, leading["manifest_id"])
    rs_row = session.get(RuleSet, leading["ruleset_id"])
    if m is None or rs_row is None or m.project_id != project_id:
        raise RunPrecondition("leading manifest/ruleset not found in project")
    rs = parse_ruleset(rs_row.source_yaml)
    lead_sid = m.definition["source_system_id"]
    cls = m.selection.get("classification", {})
    loaded = {(c["type"], n.split(":", 1)[1]) for n, c in cls.items() if c["classification"] in TRANSFER_CLASSES or (c["classification"] == "REFERENCE_ONLY" and c["type"] in _KIND_TYPE.values())}
    cands = find_duplicate_masters(session, [lead_sid, source_system_id])
    for kind, lst in cands.items():
        cands[kind] = [c for c in lst if c["survivor"]["system"] == lead_sid and (_KIND_TYPE[kind], c["survivor"]["key"]) in loaded]
    store = RecordStore.load(session, lead_sid, tables=[KINDS[k][0] for k in KINDS])

    def survivor_key(kind, key):
        table, key_field, _ = KINDS[kind]
        row = store.by_key(table, key)
        out, _ = transform_record(rs, table, row)
        return out[key_field]

    return {"candidates": {k: len(v) for k, v in cands.items()}, "lookups": dedup_lookups_for_source(cands, source_system_id, survivor_key)}

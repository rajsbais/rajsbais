"""Rule factory v2: mappings learned from the approved rule sets of other projects of the same tenant.

Every approved rule set is a reviewed statement of how a value maps (company codes, charts of accounts, plants,
controlling areas, key prefixes). When a new candidate rule set is generated, the mappings that approved sets
agree on are proposed as learned lookups with their provenance (project, rule set, version, approver); values
that approved sets map differently are listed for review and never proposed. Learned rules start as pending
decisions like every other rule: the approver of the new set decides, with the provenance in front of them.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.tables import TABLES
from ..models import Project, RuleSet
from .engine import _rule_fields

LEARNABLE_TYPES = ("org_reassign", "value_map")
# fields whose mappings are project-specific by nature and never transfer between projects
NEVER_LEARN = {"BUKRS", "BUKRS_VF", "VBUND", "WERKS", "DWERK", "BWKEY", "UMWRK", "VSTEL", "VKORG", "EKORG", "KOKRS"}


def _approved_rulesets(session: Session, tenant_id: str, exclude_project: str | None) -> list[tuple[RuleSet, Project]]:
    rows = session.execute(select(RuleSet, Project).join(Project, Project.id == RuleSet.project_id).where(Project.tenant_id == tenant_id, RuleSet.status == "APPROVED")).all()
    return [(rs, pr) for rs, pr in rows if pr.id != exclude_project]


def learn_mappings(session: Session, tenant_id: str, exclude_project: str | None = None, include_org: bool = False) -> dict:
    """{field: {"entries": {source: target}, "conflicts": {source: {target: [provenance]}}, "sources": [...]}} from the
    approved rule sets of the tenant's other projects. Organisational fields are skipped unless asked for: a
    company-code map belongs to its own deal."""
    seen: dict[str, dict[str, dict[str, list[dict]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    sources: dict[str, dict] = {}
    for rs, pr in _approved_rulesets(session, tenant_id, exclude_project):
        prov = {"project_id": pr.id, "project": pr.name, "ruleset_id": rs.id, "ruleset": rs.name, "version": rs.version, "approved_by": rs.approved_by}
        lookups = rs.compiled.get("lookups") or {}
        for r in rs.compiled.get("rules") or []:
            if r.get("type") not in LEARNABLE_TYPES:
                continue
            mapping = r.get("map") or lookups.get(r.get("lookup") or "", {})
            if not mapping:
                continue
            for f in _rule_fields(r):
                if f in NEVER_LEARN and not include_org:
                    continue
                for src, tgt in mapping.items():
                    seen[f][str(src)][str(tgt)].append({**prov, "rule": r.get("id")})
                    sources.setdefault(rs.id, prov)
    out = {}
    for f, by_src in seen.items():
        entries, conflicts, support = {}, {}, {}
        for src, by_tgt in by_src.items():
            if len(by_tgt) == 1:
                tgt, provs = next(iter(by_tgt.items()))
                entries[src] = tgt
                support[src] = len({p["ruleset_id"] for p in provs})
            else:
                conflicts[src] = {tgt: [{"project": p["project"], "ruleset": f"{p['ruleset']} v{p['version']}", "rule": p["rule"]} for p in provs] for tgt, provs in by_tgt.items()}
        srcs = sorted({p["ruleset_id"] for by_tgt in by_src.values() for provs in by_tgt.values() for p in provs})
        out[f] = {"entries": dict(sorted(entries.items())), "support": support, "conflicts": conflicts, "sources": [sources[s] for s in srcs], "tables": sorted(t for t, td in TABLES.items() if f in td.fields)}
    return {"tenant_id": tenant_id, "approved_rulesets": len({rs.id for rs, _ in _approved_rulesets(session, tenant_id, exclude_project)}), "fields": dict(sorted(out.items())), "note": "learned from approved rule sets of other projects of the tenant; organisational fields (company code, plant, sales / purchasing organisation, controlling area) are not learned unless asked for, values the approved sets map differently are listed as conflicts and never proposed"}


def learned_rules(learning: dict, existing_fields: set[str] | None = None) -> tuple[list[dict], dict, list[str]]:
    """Lookups and value_map rules from the learned mappings for the fields no generated rule covers yet, each
    rule describing where its mapping comes from; conflicts are reported, not applied."""
    existing = existing_fields or set()
    rules, lookups, review = [], {}, []
    for f, d in learning.get("fields", {}).items():
        if f in existing or not d["entries"]:
            if d["conflicts"]:
                review.append(f"{f}: {len(d['conflicts'])} value(s) mapped differently by approved rule sets; not proposed")
            continue
        name = f"learned_{f.lower()}"
        lookups[name] = dict(d["entries"])
        prov = "; ".join(f"{s['project']} / {s['ruleset']} v{s['version']} (approved by {s['approved_by']})" for s in d["sources"][:3]) + (f"; +{len(d['sources']) - 3} more" if len(d["sources"]) > 3 else "")
        rules.append({"id": f"learned-{f.lower()}", "type": "value_map", "description": f"Learned mapping for {f} from approved rule sets: {prov}. {len(d['entries'])} value(s); review before approval.", "tables": d["tables"] or ["*"], "fields": [f], "lookup": name, "on_missing": "passthrough", "learned": True})
        if d["conflicts"]:
            review.append(f"{f}: {len(d['conflicts'])} value(s) mapped differently by approved rule sets ({', '.join(list(d['conflicts'])[:5])}); left out of the lookup")
    return rules, lookups, review


__all__ = ["LEARNABLE_TYPES", "NEVER_LEARN", "learn_mappings", "learned_rules"]

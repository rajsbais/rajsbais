"""Attribute-based scoping (ABAC) on top of the role permissions.

A principal may carry attributes: `systems` (SIDs, SID/client or system ids it may touch) and `company_codes` (the company codes whose data
it may select). A missing attribute means no restriction; an empty list means nothing is allowed. Roles say WHAT a person may do; these say
ON WHAT. Checks live where systems and company codes are introduced (creation) and at the API for every path that names an existing object.

Two more attributes, `plants` and `sales_orgs`, work like company codes for what a scope NAMES (a named plant must be allowed) but are
enforced on the DATA of the plan: every row of the plan's dependency closure that carries a plant or a sales organisation (VBAP-WERKS,
MARC-WERKS, VBAK-VKORG, ...) must be inside the allowed set. The platform does not prune out-of-scope rows (a document with a missing
item is not a faithful copy): a plan that contains one is REFUSED with the documents named, and the person narrows the scope.

Covered: systems everywhere a system or an object bound to a system is named; company codes for selective manifests (and the plan's
dependency closure), delta scopes and test-data requests; plants and sales organisations for selective manifests and delta scopes (a delta
scope is dry-planned when it is created or changed); the discovery lists (company codes, plants) are filtered to the principal's scope.
NOT covered: scoping of lean-client builds, post-copy and full refresh (they act on whole systems, so only the system scope applies),
plant/sales-org scoping of test-data requests and of delta runs after approval (the scheduler runs as a service account), row-level
scoping beyond the plant and sales-organisation fields listed here, and hiding which plants exist in the system from `/api/systems/*/readiness`.
"""
from __future__ import annotations

from .auth import Forbidden, Principal


ATTRS = ("systems", "company_codes", "plants", "sales_orgs")
# the plan rows that carry a plant / a sales organisation (table -> field); the closure of a plan is checked against the allowed sets
PLANT_FIELDS = {"VBAP": "WERKS", "LIPS": "WERKS", "LIKP": "WERKS", "MARC": "WERKS", "EKPO": "WERKS", "AUFK": "WERKS", "STKO": "WERKS", "MSEG": "WERKS", "MATDOC": "WERKS"}
SALES_ORG_FIELDS = {"VBAK": "VKORG", "KNVV": "VKORG"}


def restricted(p: Principal) -> bool:
    return any(p.attrs.get(a) is not None for a in ATTRS)


def require_named(p: Principal, scope, what: str) -> None:
    """Plants and sales organisations a scope NAMES must be allowed. (What it does not name is decided by the plan's closure.)"""
    for attr, field_ in (("plants", "plants"), ("sales_orgs", "sales_orgs")):
        allowed = p.attrs.get(attr)
        named = list(getattr(scope, field_, []) or [])
        if allowed is not None and any(v not in allowed for v in named):
            raise Forbidden(f"outside your scope: {what} names {field_.replace('_', ' ')} {sorted(set(named) - set(allowed))} but you may only use {sorted(allowed)}")


def system_ok(p: Principal, s) -> bool:
    allowed = p.attrs.get("systems")
    return allowed is None or s.id in allowed or s.sid in allowed or f"{s.sid}/{s.client}" in allowed


def require_systems(svc, p: Principal, *ids) -> None:
    for i in ids:
        s = svc.systems.get(i) if i else None
        if s is not None and not system_ok(p, s):
            raise Forbidden(f"outside your scope: you may not use system {s.label}")


def companies_ok(p: Principal, codes) -> bool:
    allowed = p.attrs.get("company_codes")
    if allowed is None:
        return True
    return bool(codes) and all(c in allowed for c in codes)  # an unbounded selection is never inside a bounded scope


def require_companies(p: Principal, codes, what: str) -> None:
    if not companies_ok(p, list(codes or [])):
        allowed = p.attrs.get("company_codes")
        raise Forbidden(f"outside your scope: {what} must name company code(s) within {sorted(allowed)}; got {sorted(codes) if codes else 'none (unbounded)'}")


def _closure_rows(p: Principal, plan) -> None:
    """Every plan row that carries a plant or a sales organisation must be inside the principal's allowed sets; offenders are named."""
    for attr, fields, noun in (("plants", PLANT_FIELDS, "plant"), ("sales_orgs", SALES_ORG_FIELDS, "sales organisation")):
        allowed = p.attrs.get(attr)
        if allowed is None:
            continue
        bad: dict[str, set] = {}
        for iid, inst in plan.instances.items():
            for table, field_ in fields.items():
                for r in inst.rows.get(table, []):
                    if r.get(field_) not in allowed:
                        bad.setdefault(iid, set()).add(r.get(field_))
        if bad:
            vals = sorted({v for vs in bad.values() for v in vs if v})
            raise Forbidden(f"outside your scope: {len(bad)} object(s) in the plan contain {noun}(s) {vals} which you may not select "
                            f"(you may use {sorted(allowed)}), for example {sorted(bad)[:3]}. The platform does not copy partial documents: narrow the scope")


def require_plan(p: Principal, plan, plant_company: dict | None = None) -> None:
    """A plan pulls in dependencies (partner documents, plants and their customizing): none may belong to a company, plant or sales
    organisation outside the scope."""
    _closure_rows(p, plan)
    allowed_plants = p.attrs.get("plants")
    if allowed_plants is not None:
        stray = sorted(set(plan.config_refs.get("PLANT", set())) - set(allowed_plants))
        if stray:
            raise Forbidden(f"outside your scope: the plan depends on plant(s) {stray} which you may not select")
    allowed = p.attrs.get("company_codes")
    if allowed is None:
        return
    extra = sorted(set(plan.config_refs.get("COMPANY_CODE", set())) - set(allowed))
    if extra:
        raise Forbidden(f"outside your scope: the plan depends on company code(s) {extra} which you may not select")
    stray = sorted(w for w in plan.config_refs.get("PLANT", set()) if (plant_company or {}).get(w) not in allowed)
    if stray:
        raise Forbidden(f"outside your scope: the plan depends on plant(s) {stray} that belong to a company you may not select "
                        f"({sorted({(plant_company or {}).get(w, '?') for w in stray})})")


def visible(svc, p: Principal, *ids) -> bool:
    return all(system_ok(p, svc.systems[i]) for i in ids if i and i in svc.systems)


def _ids(svc, prefix: str, key: str) -> list:
    """System ids that an existing object named in an API path is bound to."""
    try:
        if prefix == "/api/projects/":
            p = svc.projects[key]
            return [p.source_id, p.target_id]
        if prefix == "/api/runs/":
            p = svc.projects[svc.runs[key].project_id]
            return [p.source_id, p.target_id]
        if prefix in ("/api/systems/", "/api/postcopy/systems/", "/api/orchestration/windows/", "/api/lean/clients/"):
            return [key]
        if prefix == "/api/postcopy/profiles/":
            return [svc.postcopy.profiles[key].system_id]
        if prefix == "/api/postcopy/runs/":
            r = svc.postcopy.runs[key]
            return [r.target_id, r.source_id]
        if prefix == "/api/delta/scenarios/":
            d = svc.delta.scenarios[key]
            return [d.source_id, d.target_id]
        if prefix == "/api/tdm/datasets/":
            return [svc.tdm.datasets[key].target_id]
        if prefix == "/api/tdm/requests/":
            return [svc.tdm.requests[key]["spec"]["target_id"]]
        if prefix == "/api/tdm/policies/":
            return [svc.tdm.policies[key].target_id]
        if prefix == "/api/lean/builds/":
            b = svc.lean.builds[key]
            return [b.host_id, b.source_id]
        if prefix == "/api/full-refresh/programs/":
            f = svc.full.programs[key]
            return [f.source_id, f.target_id]
        if prefix == "/api/orchestration/jobs/":
            return [svc.orch.jobs[key].target_id]
    except KeyError:
        return []  # unknown object: the endpoint answers 404, nothing to protect
    return []


PREFIXES = ("/api/projects/", "/api/runs/", "/api/systems/", "/api/postcopy/systems/", "/api/orchestration/windows/", "/api/lean/clients/",
            "/api/postcopy/profiles/", "/api/postcopy/runs/", "/api/delta/scenarios/", "/api/tdm/datasets/", "/api/tdm/requests/", "/api/tdm/policies/",
            "/api/lean/builds/", "/api/full-refresh/programs/", "/api/orchestration/jobs/")


QUERY_KEYS = ("source_id", "target_id", "system_id", "host_id")


def check_path(svc, p: Principal, path: str, query=None) -> None:
    if not restricted(p):
        return
    require_systems(svc, p, *[query.get(k) for k in QUERY_KEYS] if query else [])
    for prefix in PREFIXES:
        if path.startswith(prefix):
            key = path[len(prefix):].split("/")[0]
            if key and key not in ("demo", "templates", "catalog"):
                require_systems(svc, p, *_ids(svc, prefix, key))
            return


def filter_discovery(p: Principal, d: dict) -> dict:
    """Company codes and plants a restricted principal may not select are not listed."""
    cc, pl = p.attrs.get("company_codes"), p.attrs.get("plants")
    if cc is not None:
        d["company_codes"] = [c for c in d.get("company_codes", []) if c["code"] in cc]
    if pl is not None or cc is not None:
        d["plants"] = [x for x in d.get("plants", []) if (pl is None or x["plant"] in pl) and (cc is None or x.get("company_code") in cc)]
    if cc is not None or pl is not None:
        d["custom_fields"] = d.get("custom_fields", [])
    return d


HR_TYPES = {"EMPLOYEE"}


def require_hr(p: Principal, object_type: str) -> None:
    """HR data is special-category personal data: only a person holding hr:copy may even name it as a root object."""
    if object_type in HR_TYPES and not p.can("hr:copy"):
        raise Forbidden("hr:copy is required to select HR (employee) data")


def require_hr_plan(p: Principal, plan) -> None:
    from ..masking.engine import HR_TABLES
    if any(t in HR_TABLES for inst in plan.instances.values() for t in inst.rows) and not p.can("hr:copy"):
        raise Forbidden("hr:copy is required: this plan contains HR data")


def hr_masking_violations(plan, policy) -> list[str]:
    """HR fields must be masked by PER-RUN ANONYMIZATION. Stable pseudonyms (reversible by whoever holds the key and candidate values) are not enough."""
    from ..masking.engine import HR_TABLES, MaskMode
    tables = {t for inst in plan.instances.values() for t in inst.rows} & HR_TABLES
    if not tables:
        return []
    from ..masking.engine import CATALOG
    need = {(t, f) for (t, f) in CATALOG if t in tables}
    rules = {(r.table, r.field): r for r in (policy.rules if policy else [])}
    bad = []
    for key in sorted(need):
        r = rules.get(key)
        if r is None:
            bad.append(f"{key[0]}.{key[1]} has no masking rule")
        elif r.mode != MaskMode.ANONYMIZE:
            bad.append(f"{key[0]}.{key[1]} is {r.mode.value}, HR requires ANONYMIZE")
    return bad

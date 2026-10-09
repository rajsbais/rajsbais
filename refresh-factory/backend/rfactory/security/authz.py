"""Attribute-based scoping (ABAC) on top of the role permissions.

A principal may carry attributes: `systems` (SIDs, SID/client or system ids it may touch) and `company_codes` (the company codes whose data
it may select). A missing attribute means no restriction; an empty list means nothing is allowed. Roles say WHAT a person may do; these say
ON WHAT. Checks live where systems and company codes are introduced (creation) and at the API for every path that names an existing object.

Covered: systems everywhere a system or an object bound to a system is named; company codes for selective manifests (and the plan's
dependency closure), delta scopes and test-data requests. NOT covered: company-code scoping of lean-client builds, post-copy and full
refresh (they act on whole systems, so only the system scope applies), plant/sales-org scoping, row-level scoping inside a system.
"""
from __future__ import annotations

from .auth import Forbidden, Principal


def restricted(p: Principal) -> bool:
    return p.attrs.get("systems") is not None or p.attrs.get("company_codes") is not None


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


def require_plan(p: Principal, plan, plant_company: dict | None = None) -> None:
    """A plan pulls in dependencies (partner documents, plants and their customizing): none may belong to a company outside the scope."""
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

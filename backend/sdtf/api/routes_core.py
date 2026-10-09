"""Auth, projects, systems, records, discovery, graph."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit.service import record_event
from ..catalog.business_objects import BUSINESS_OBJECTS, RELATIONSHIPS, S4_COMPATIBILITY
from ..catalog.store import RecordStore, import_tables
from ..catalog.tables import TABLES
from ..db import get_db
from ..demo import create_demo_project
from ..discovery.service import discover_system, latest_snapshot
from ..graph.service import TraversalPolicy, build_graph, persist_graph
from ..models import (
    BusinessObjectInstance,
    MigrationRun,
    OrgUnit,
    Project,
    SapRecord,
    SapSystem,
    ScopeManifest,
    TableStatistic,
)
from ..runtime.adapters import ADAPTER_REGISTRY
from ..security.auth import (
    Principal,
    assert_project_access,
    authenticate,
    current_principal,
    issue_token,
    mask_payload,
    require,
)
from ..synthetic.ecc_generator import LandscapeSpec, generate_landscape
from .deps import get_system

router = APIRouter()


# ------------------------------------------------------------------------------------------- auth
class TokenRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/token", tags=["auth"])
def token(req: TokenRequest, db: Session = Depends(get_db)):
    u = authenticate(db, req.username, req.password)
    if u is None:
        raise HTTPException(401, "invalid credentials")
    record_event(db, u.username, "LOGIN", "USER", u.id, {})
    return {"access_token": issue_token(u), "token_type": "bearer", "username": u.username, "roles": u.roles, "display_name": u.display_name}


@router.get("/auth/me", tags=["auth"])
def me(p: Principal = Depends(current_principal)):
    return {"username": p.username, "roles": p.roles, "tenant_id": p.tenant_id}


# ---------------------------------------------------------------------------------------- projects
class ProjectCreate(BaseModel):
    name: str
    scenario_type: str = Field(pattern="^(CARVE_OUT|SDT|MERGER|BLUEFIELD)$")
    description: str = ""


class DemoCreate(BaseModel):
    scale: int = Field(1, ge=1, le=10)
    seed: int = 42
    name: str = "Project Aurora - Specialty Materials carve-out"


def _project_out(db: Session, p: Project) -> dict:
    systems = db.execute(select(SapSystem).where(SapSystem.project_id == p.id)).scalars().all()
    manifests = db.execute(select(func.count(ScopeManifest.id)).where(ScopeManifest.project_id == p.id)).scalar() or 0
    runs = db.execute(select(MigrationRun).where(MigrationRun.project_id == p.id).order_by(MigrationRun.created_at.desc())).scalars().all()
    last = runs[0] if runs else None
    return {"id": p.id, "name": p.name, "scenario_type": p.scenario_type, "description": p.description, "status": p.status, "created_by": p.created_by, "created_at": p.created_at, "meta": p.meta, "systems": [_system_out(s) for s in systems], "manifests": manifests, "runs": len(runs), "last_run": {"id": last.id, "status": last.status, "reconciliation": (last.report or {}).get("reconciliation", {}).get("overall")} if last else None}


def _system_out(s: SapSystem) -> dict:
    return {"id": s.id, "project_id": s.project_id, "sid": s.sid, "client": s.client, "role": s.role, "product": s.product, "release": s.release, "database": s.database, "os_name": s.os_name, "connector": s.connector, "connector_status": s.connector_status, "logical_system": s.logical_system, "meta": s.meta}


@router.get("/projects", tags=["projects"])
def list_projects(db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    rows = db.execute(select(Project).where(Project.tenant_id == p.tenant_id).order_by(Project.created_at.desc())).scalars().all()
    return [_project_out(db, r) for r in rows]


@router.post("/projects", tags=["projects"], status_code=201)
def create_project(req: ProjectCreate, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    proj = Project(name=req.name, scenario_type=req.scenario_type, description=req.description, created_by=p.username, tenant_id=p.tenant_id)
    db.add(proj)
    db.flush()
    record_event(db, p.username, "PROJECT_CREATED", "PROJECT", proj.id, {"name": req.name})
    return _project_out(db, proj)


@router.post("/projects/demo", tags=["projects"], status_code=201)
def create_demo(req: DemoCreate, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    ctx = create_demo_project(db, p.username, scale=req.scale, seed=req.seed, name=req.name)
    ctx["project"].tenant_id = p.tenant_id
    db.flush()
    return {"project": _project_out(db, ctx["project"]), "import_counts": ctx["import_counts"]}


@router.get("/projects/{project_id}", tags=["projects"])
def get_project(project_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    return _project_out(db, assert_project_access(db, p, project_id))


class SystemCreate(BaseModel):
    sid: str
    client: str = "100"
    role: str = Field(pattern="^(SOURCE|TARGET)$")
    product: str = Field(pattern="^(ECC|S4HANA)$")
    release: str
    connector: str = "SYNTHETIC"
    database: str = ""
    os_name: str = ""


@router.post("/projects/{project_id}/systems", tags=["systems"], status_code=201)
def create_system(project_id: str, req: SystemCreate, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    assert_project_access(db, p, project_id)
    if req.connector not in ADAPTER_REGISTRY:
        raise HTTPException(400, f"unknown connector {req.connector}")
    s = SapSystem(project_id=project_id, sid=req.sid, client=req.client, role=req.role, product=req.product, release=req.release, connector=req.connector, connector_status=ADAPTER_REGISTRY[req.connector]["status"], database=req.database, os_name=req.os_name, logical_system=f"{req.sid}CLNT{req.client}")
    db.add(s)
    db.flush()
    record_event(db, p.username, "SYSTEM_REGISTERED", "SYSTEM", s.id, {"sid": s.sid, "connector": s.connector})
    return _system_out(s)


class SyntheticImport(BaseModel):
    scale: int = Field(1, ge=1, le=10)
    seed: int = 42


@router.post("/systems/{system_id}/import-synthetic", tags=["systems"])
def import_synthetic(req: SyntheticImport, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    if s.connector != "SYNTHETIC":
        raise HTTPException(409, "synthetic import is only possible for SYNTHETIC systems")
    counts = import_tables(db, s.id, generate_landscape(LandscapeSpec(seed=req.seed, scale=req.scale)))
    record_event(db, p.username, "SYNTHETIC_IMPORTED", "SYSTEM", s.id, {"rows": sum(counts.values())})
    return {"system_id": s.id, "import_counts": counts, "rows": sum(counts.values())}


@router.get("/systems/{system_id}/records", tags=["systems"])
def browse_records(table: str, s: SapSystem = Depends(get_system), bukrs: str | None = None, limit: int = Query(50, le=500), offset: int = 0, db: Session = Depends(get_db), p: Principal = Depends(require("records:read"))):
    stmt = select(SapRecord).where(SapRecord.system_id == s.id, SapRecord.table_name == table)
    if bukrs:
        stmt = stmt.where(SapRecord.bukrs == bukrs)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar()
    rows = db.execute(stmt.order_by(SapRecord.id).offset(offset).limit(limit)).scalars().all()
    return {"table": table, "total": total, "masked": not p.has("data:unmasked"), "rows": [{"key": r.record_key, **mask_payload(r.payload, p)} for r in rows]}


@router.get("/systems/{system_id}/tables", tags=["systems"])
def system_tables(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    rows = db.execute(select(SapRecord.table_name, func.count()).where(SapRecord.system_id == s.id).group_by(SapRecord.table_name)).all()
    return [{"table": t, "rows": n, "description": TABLES[t].description if t in TABLES else "", "s4_status": TABLES[t].s4_status if t in TABLES else "UNKNOWN"} for t, n in sorted(rows)]


# --------------------------------------------------------------------------------------- discovery
@router.post("/systems/{system_id}/discover", tags=["discovery"])
def run_discovery(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    snap = discover_system(db, s, p.username)
    record_event(db, p.username, "DISCOVERY_COMPLETED", "SYSTEM", s.id, {"snapshot": snap.id})
    return {"snapshot_id": snap.id, "summary": snap.summary}


@router.get("/systems/{system_id}/discovery", tags=["discovery"])
def get_discovery(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    snap = latest_snapshot(db, s.id)
    if snap is None:
        raise HTTPException(404, "no discovery snapshot; run discovery first")
    return {"snapshot_id": snap.id, "created_at": snap.created_at, "created_by": snap.created_by, "summary": snap.summary}


@router.get("/systems/{system_id}/org-structure", tags=["discovery"])
def org_structure(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    units = db.execute(select(OrgUnit).where(OrgUnit.system_id == s.id)).scalars().all()
    out = [{"type": u.unit_type, "code": u.code, "name": u.name, "parent_type": u.parent_type, "parent_code": u.parent_code, "attributes": u.attributes} for u in units]
    # tree: controlling area -> company code -> plants/sales orgs/purch orgs/cost centers/profit centers
    by_cc: dict[str, dict] = {}
    for u in out:
        if u["type"] == "COMPANY_CODE":
            by_cc[u["code"]] = {**u, "children": []}
    for u in out:
        if u["parent_type"] == "COMPANY_CODE" and u["parent_code"] in by_cc:
            by_cc[u["parent_code"]]["children"].append(u)
    areas: dict[str, dict] = {}
    for u in out:
        if u["type"] == "CONTROLLING_AREA":
            areas[u["code"]] = {**u, "children": []}
    for cc in by_cc.values():
        k = cc["attributes"].get("KOKRS")
        if k in areas:
            areas[k]["children"].append(cc)
    return {"units": out, "tree": list(areas.values()), "counts": {t: sum(1 for u in out if u["type"] == t) for t in {u["type"] for u in out}}}


@router.get("/systems/{system_id}/table-statistics", tags=["discovery"])
def table_statistics(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    snap = latest_snapshot(db, s.id)
    if snap is None:
        return []
    rows = db.execute(select(TableStatistic).where(TableStatistic.snapshot_id == snap.id).order_by(TableStatistic.row_count.desc())).scalars().all()
    return [{"table": t.table_name, "description": TABLES[t.table_name].description if t.table_name in TABLES else "", "custom": t.is_custom, "rows": t.row_count, "est_bytes": t.est_bytes, "by_company_code": t.by_company_code, "by_fiscal_year": t.by_fiscal_year, "key_fields": t.key_fields, "fields": t.fields, "s4_status": TABLES[t.table_name].s4_status if t.table_name in TABLES else "UNKNOWN", "s4_note": TABLES[t.table_name].s4_note if t.table_name in TABLES else ""} for t in rows]


@router.get("/systems/{system_id}/business-objects", tags=["discovery"])
def business_objects(s: SapSystem = Depends(get_system), object_type: str | None = None, bukrs: str | None = None, limit: int = Query(100, le=1000), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    stmt = select(BusinessObjectInstance).where(BusinessObjectInstance.system_id == s.id)
    if object_type:
        stmt = stmt.where(BusinessObjectInstance.object_type == object_type)
    if bukrs:
        stmt = stmt.where(BusinessObjectInstance.bukrs == bukrs)
    rows = db.execute(stmt.limit(limit)).scalars().all()
    counts = db.execute(select(BusinessObjectInstance.object_type, func.count()).where(BusinessObjectInstance.system_id == s.id).group_by(BusinessObjectInstance.object_type)).all()
    return {"counts": {t: n for t, n in counts}, "items": [{"type": r.object_type, "key": r.object_key, "bukrs": r.bukrs, "werks": r.werks, "gjahr": r.gjahr, "status": r.status, "company_codes": r.company_codes} for r in rows]}


# ------------------------------------------------------------------------------------------- graph
@router.post("/systems/{system_id}/graph/build", tags=["graph"])
def graph_build(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    store = RecordStore.load(db, s.id)
    stats = persist_graph(db, s.id, build_graph(store, s.id))
    record_event(db, p.username, "GRAPH_BUILT", "SYSTEM", s.id, stats)
    return stats


@router.get("/systems/{system_id}/graph/stats", tags=["graph"])
def graph_stats(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..graph.store import get_graph_store

    return {**get_graph_store(db).stats(s.id), "relationship_model": [{"from": r.from_type, "to": r.to_type, "edge": r.edge_type, "name": r.name, "description": r.description} for r in RELATIONSHIPS]}


@router.get("/systems/{system_id}/graph/nodes", tags=["graph"])
def graph_nodes(s: SapSystem = Depends(get_system), node_type: str | None = None, q: str | None = None, limit: int = Query(50, le=500), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..graph.store import get_graph_store

    return get_graph_store(db).search_nodes(s.id, node_type, q, limit)


@router.get("/systems/{system_id}/graph/neighbourhood", tags=["graph"])
def graph_neighbourhood(node: str, depth: int = Query(2, ge=1, le=4), s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..graph.store import get_graph_store

    store = get_graph_store(db)
    if not store.has_node(s.id, node):
        raise HTTPException(404, "node not found")
    return store.neighbourhood(s.id, node, depth)


class TraverseRequest(BaseModel):
    seeds: list[str]
    edge_policies: dict[str, str] = Field(default_factory=dict)
    type_policies: dict[str, str] = Field(default_factory=dict)
    max_depth: int = 8


@router.post("/systems/{system_id}/graph/traverse", tags=["graph"])
def graph_traverse(req: TraverseRequest, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    from ..graph.store import get_graph_store

    pol = TraversalPolicy(max_depth=req.max_depth)
    pol.edge_policies.update(req.edge_policies)
    pol.type_policies.update(req.type_policies)
    res = get_graph_store(db).traverse(s.id, req.seeds, pol)
    return {"included": res.included, "traces": res.traces[:2000], "stopped": res.stopped[:500], "missing": res.missing}


# ------------------------------------------------------------------------------------------ catalog
@router.get("/catalog/business-objects", tags=["catalog"])
def catalog_business_objects(p: Principal = Depends(current_principal)):
    return [{"id": b.id, "name": b.name, "domain": b.domain, "kind": b.kind, "header_table": b.header_table, "item_tables": list(b.item_tables), "key_fields": list(b.key_fields), "org_scope": b.org_scope, "applicability": list(b.applicability), "load_methods": [m.__dict__ for m in b.load_methods], "s4_simplification": b.s4_simplification} for b in BUSINESS_OBJECTS.values()]


@router.get("/catalog/tables", tags=["catalog"])
def catalog_tables(p: Principal = Depends(current_principal)):
    return [{"name": t.name, "description": t.description, "key_fields": list(t.key_fields), "fields": list(t.fields), "org_field": t.org_field, "domain": t.domain, "s4_status": t.s4_status, "s4_note": t.s4_note} for t in TABLES.values()]


@router.get("/catalog/s4-compatibility", tags=["catalog"])
def s4_compat(p: Principal = Depends(current_principal)):
    return S4_COMPATIBILITY


@router.get("/catalog/adapters", tags=["catalog"])
def adapters(p: Principal = Depends(current_principal)):
    return {k: {"status": v["status"], "description": v["description"]} for k, v in ADAPTER_REGISTRY.items()}

"""Auth, projects, systems, records, discovery, graph."""
from __future__ import annotations

import time

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


# OIDC browser login (authorization code + PKCE). The SPA generates verifier/challenge/state/nonce, sends the
# user to the provider, and on return hands the code to the API which exchanges and verifies it.
class OidcExchange(BaseModel):
    code: str = ""
    code_verifier: str = ""
    redirect_uri: str = ""
    nonce: str | None = None
    tokens: dict | None = None  # SDTF_OIDC_EXCHANGE=browser: the SPA already holds the token response


class OidcRefresh(BaseModel):
    refresh_token: str


@router.get("/auth/oidc/config", tags=["auth"])
def oidc_config():
    from ..config import settings
    from ..security import oidc

    return {**oidc.public_config(), "dev_login": settings.dev_users_enabled}


@router.post("/auth/oidc/exchange", tags=["auth"])
def oidc_exchange(req: OidcExchange, db: Session = Depends(get_db)):
    from ..security import oidc

    try:
        if req.tokens is not None:
            out = oidc.verify_browser_tokens(req.tokens, req.nonce)
        else:
            if not (req.code and req.code_verifier and req.redirect_uri):
                raise HTTPException(422, "code, code_verifier and redirect_uri are required")
            out = oidc.exchange_code(req.code, req.code_verifier, req.redirect_uri, req.nonce)
    except oidc.OidcError as e:
        raise HTTPException(401, f"OIDC login failed: {e}") from None
    record_event(db, out["username"], "LOGIN", "USER", out["username"], {"method": "oidc", "idp": oidc.config().issuer, "token_kind": out["token_kind"]}, tenant_id=out["tenant_id"])
    return out


@router.post("/auth/oidc/refresh", tags=["auth"])
def oidc_refresh(req: OidcRefresh):
    from ..security import oidc

    try:
        return oidc.refresh_tokens(req.refresh_token)
    except oidc.OidcError as e:
        raise HTTPException(401, f"OIDC refresh failed: {e}") from None


# ---------------------------------------------------------------------------------------- projects
class ProjectCreate(BaseModel):
    name: str
    scenario_type: str = Field(pattern="^(CARVE_OUT|SDT|MERGER|BLUEFIELD)$")
    description: str = ""


class DemoCreate(BaseModel):
    scale: int = Field(1, ge=1, le=10)
    seed: int = 42
    connector: str = Field("SYNTHETIC", pattern="^(SYNTHETIC|RFC)$")
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
    ctx = create_demo_project(db, p.username, scale=req.scale, seed=req.seed, name=req.name, connector=req.connector)
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
    meta: dict = Field(default_factory=dict)  # RFC: {"rfc": {"transport": "simulated" | "pyrfc", "dest": {...}, "allowed_tables": [...]}}


def _connector_status(connector: str, meta: dict) -> str:
    if connector == "RFC":
        return "SIMULATED" if (meta.get("rfc") or {}).get("transport") == "simulated" else ADAPTER_REGISTRY["RFC"]["status"]
    if connector == "API":
        return "SIMULATED" if (meta.get("api") or {}).get("transport") == "simulated" else ADAPTER_REGISTRY["API"]["status"]
    return ADAPTER_REGISTRY[connector]["status"]


def _uses_record_store(s: SapSystem) -> bool:
    return s.connector == "SYNTHETIC" or (s.connector == "RFC" and (s.meta.get("rfc") or {}).get("transport") == "simulated") or (s.connector == "API" and (s.meta.get("api") or {}).get("transport") == "simulated")


@router.post("/projects/{project_id}/systems", tags=["systems"], status_code=201)
def create_system(project_id: str, req: SystemCreate, db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    assert_project_access(db, p, project_id)
    if req.connector not in ADAPTER_REGISTRY:
        raise HTTPException(400, f"unknown connector {req.connector}")
    from ..runtime.rfc import SECRET_KEYS
    from ..runtime.target_api import SECRET_KEYS as API_SECRET_KEYS

    meta = dict(req.meta)
    if any(k in (meta.get("rfc", {}).get("dest") or {}) for k in SECRET_KEYS):
        raise HTTPException(400, "RFC secrets are never stored: reference them as 'env:NAME' or set SDTF_RFC_DEST_<SID>_PASSWD")
    if any(k in (meta.get("api", {}).get("dest") or {}) for k in API_SECRET_KEYS):
        raise HTTPException(400, "API secrets are never stored: reference them as 'env:NAME' or set SDTF_S4_API_<SID>_PASSWD")
    if req.connector == "API" and req.role != "TARGET":
        raise HTTPException(400, "the API connector loads targets; sources use SYNTHETIC or RFC")
    s = SapSystem(project_id=project_id, sid=req.sid, client=req.client, role=req.role, product=req.product, release=req.release, connector=req.connector, connector_status=_connector_status(req.connector, meta), database=req.database, os_name=req.os_name, logical_system=f"{req.sid}CLNT{req.client}", meta=meta)
    db.add(s)
    db.flush()
    record_event(db, p.username, "SYSTEM_REGISTERED", "SYSTEM", s.id, {"sid": s.sid, "connector": s.connector})
    return _system_out(s)


def _test_api_connector(s: SapSystem, db: Session, p: Principal) -> dict:
    """Fetch a CSRF token from API_BUSINESS_PARTNER and read one company code's configuration through the target
    transport: proves reachability, authentication and the service activation without writing anything."""
    from ..runtime import target_api as tapi

    t0 = time.monotonic()
    try:
        transport = tapi.make_target_transport(db, s)
        client = tapi.TargetApiClient(transport)
        token = client._token("API_BUSINESS_PARTNER")
        services = sorted({b.service for b in tapi.API_BINDINGS.values()})
        probe = None
        if hasattr(transport, "rows"):
            cc = next((r["BUKRS"] for r in transport.rows("T001")), None)
            probe = {"company_code": cc, "sales_orgs": len(transport.rows("TVKO")), "plants": len(transport.rows("T001W"))}
        out = {"ok": True, "connector": "API", "transport": getattr(transport, "name", "?"), "csrf_token": bool(token), "services": services, "numbering": getattr(transport, "numbering", None), "probe": probe, "destination": tapi.mask_api_destination(tapi.resolve_api_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
        from ..reconciliation.views import target_has_rfc

        if target_has_rfc(s):  # the target also hosts the read-only add-on: the reconciliation reads what the APIs cannot serve through it
            from ..runtime import rfc as rfcmod

            t1 = time.monotonic()
            try:
                rt = rfcmod.make_transport(s.sid, s.meta, store_loader=lambda: RecordStore.load(db, s.id, tables=["T001", "T001K"]))
                rc = rfcmod.AbapAddonClient(rt, package_size=5)
                snap = rc.open_snapshot(["T001"])
                rows, _cursor, eof = rc.read_package("T001", [])
                try:
                    agg = {"available": True, "rows": rc.count("T001", [])}
                except rfcmod.RfcError as e:
                    agg = {"available": False, "error": e.key}
                out["rfc_readback"] = {"ok": True, "transport": getattr(rt, "name", "?"), "snapshot": snap, "sample_rows": len(rows), "eof": eof, "checksum_verified": True, "aggregate": agg, "destination": rfcmod.mask_destination(rfcmod.resolve_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t1) * 1000, 1)}
            except rfcmod.RfcError as e:
                out["rfc_readback"] = {"ok": False, "error": e.key, "detail": e.message, "destination": rfcmod.mask_destination(rfcmod.resolve_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t1) * 1000, 1)}
    except tapi.ApiError as e:
        out = {"ok": False, "connector": "API", "error": e.code, "detail": e.message, "destination": tapi.mask_api_destination(tapi.resolve_api_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
    record_event(db, p.username, "CONNECTOR_TESTED", "SYSTEM", s.id, {k: v for k, v in out.items() if k in ("ok", "transport", "error")})
    return out


class MetadataCheckIn(BaseModel):
    service: str = Field(min_length=3, max_length=60)
    content: str = Field(min_length=10, description="the service's $metadata (EDMX) as downloaded from the target or the SAP API hub")


@router.post("/metadata/check", tags=["systems"])
def metadata_check_file(req: MetadataCheckIn, p: Principal = Depends(require("project:read"))):
    """Check the platform's bindings for one service against an EDMX document (no system needed)."""
    from ..runtime import metadata_check as mc

    try:
        md = mc.parse_edmx(req.content)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    res = mc.check_service(md, req.service)
    return {**res, "markdown": mc.report_markdown(res)}


@router.get("/metadata/expectations", tags=["systems"])
def metadata_expectations(service: str | None = None, p: Principal = Depends(require("project:read"))):
    """What the platform relies on per service: entity sets, properties, keys (the list a $metadata check verifies)."""
    from ..runtime import metadata_check as mc

    services = [service] if service else mc.bound_services()
    return {"services": {s: mc.expectations(s) for s in services}}


@router.post("/systems/{system_id}/connector/metadata-check", tags=["systems"])
def connector_metadata_check(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Fetch the gateway catalogue and the $metadata of every bound service from an API target and check the
    bindings against them (reads only)."""
    from ..runtime import metadata_check as mc
    from ..runtime import target_api as tapi

    if s.connector != "API":
        raise HTTPException(409, f"metadata check is only defined for API targets (this one is {s.connector})")
    try:
        transport = tapi.make_target_transport(db, s)
    except tapi.ApiError as e:
        raise HTTPException(502, f"{e.code}: {e.message}") from None
    res = mc.check_target(transport)
    record_event(db, p.username, "METADATA_CHECKED", "SYSTEM", s.id, {"transport": res["transport"], **res["summary"]})
    return {**res, "markdown": mc.report_markdown(res)}


@router.post("/systems/{system_id}/connector/test", tags=["systems"])
def test_connector(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Open a snapshot, read the metadata of T001 and one small package through the system's RFC transport.
    Proves connectivity, authorization and the contract end to end; never touches application data beyond T001."""
    from ..runtime import rfc as rfcmod

    if s.connector == "API":
        return _test_api_connector(s, db, p)
    if s.connector != "RFC":
        raise HTTPException(409, f"connector test is only defined for RFC sources and API targets (this one is {s.connector})")
    t0 = time.monotonic()
    try:
        transport = rfcmod.make_transport(s.sid, s.meta, store_loader=lambda: RecordStore.load(db, s.id, tables=["T001", "T001K"]))
        client = rfcmod.AbapAddonClient(transport, package_size=5)
        snap = client.open_snapshot(["T001"])
        meta = client.table_metadata("T001")
        rows, cursor, eof = client.read_package("T001", [])
        try:
            aggregate = {"available": True, "rows": client.count("T001", [])}
        except rfcmod.RfcError as e:
            aggregate = {"available": False, "error": e.key, "note": "Z_SDTF_AGGREGATE missing or not authorised: reconciliation reads rows without read-integrity evidence"}
        out = {"ok": True, "connector": "RFC", "transport": getattr(transport, "name", "?"), "snapshot": snap, "valid_until": client.valid_until, "table": meta, "sample_rows": len(rows), "eof": eof, "checksum_verified": True, "aggregate": aggregate, "destination": rfcmod.mask_destination(rfcmod.resolve_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
    except rfcmod.RfcError as e:
        out = {"ok": False, "connector": "RFC", "error": e.key, "detail": e.message, "destination": rfcmod.mask_destination(rfcmod.resolve_destination(s.sid, s.meta)), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
    record_event(db, p.username, "CONNECTOR_TESTED", "SYSTEM", s.id, {k: v for k, v in out.items() if k in ("ok", "transport", "error", "snapshot")})
    return out


@router.get("/systems/{system_id}/read-config", tags=["systems"])
def get_read_config(s: SapSystem = Depends(get_system), p: Principal = Depends(require("project:read"))):
    """How the reconciliation reads this system over RFC: journal table and ledger, asset and inventory chains
    (stored subset, effective values with defaults, options for the form)."""
    from ..reconciliation import read_config as rc

    return rc.describe(s)


@router.put("/systems/{system_id}/read-config", tags=["systems"])
def put_read_config(payload: dict, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Replace the read configuration (validated; only the four read keys of meta.rfc change, transport and
    destination are never touched; an empty object resets to the defaults). Audited."""
    from ..reconciliation import read_config as rc

    try:
        out = rc.apply(s, payload or {})
    except rc.ReadConfigError as e:
        raise HTTPException(400, str(e)) from None
    db.flush()
    record_event(db, p.username, "READ_CONFIG_CHANGED", "SYSTEM", s.id, {"configured": out["configured"]})
    return out


RFC_DEST_KEYS = ("ashost", "sysnr", "client", "user", "lang", "saprouter", "mshost", "msserv", "group", "sysid", "trace")
API_DEST_KEYS = ("base_url", "client", "user", "verify_tls", "timeout")


class DestinationIn(BaseModel):
    transport: str | None = Field(None, description="RFC: simulated | pyrfc; API: simulated | https")
    dest: dict = Field(default_factory=dict, description="connection parameters without secrets; passwd may only be an 'env:NAME' reference")


def _destination_out(s: SapSystem) -> dict:
    from ..runtime import rfc as rfcmod
    from ..runtime import target_api as tapi

    rfc = (s.meta or {}).get("rfc") or {}
    api = (s.meta or {}).get("api") or {}
    out = {"system_id": s.id, "sid": s.sid, "role": s.role, "connector": s.connector, "connector_status": s.connector_status}
    if s.connector == "RFC" or rfc:
        out["rfc"] = {"transport": rfc.get("transport") or ("pyrfc" if s.connector == "RFC" else None), "dest": rfcmod.mask_destination(dict(rfc.get("dest") or {})), "resolved": rfcmod.mask_destination(rfcmod.resolve_destination(s.sid, s.meta)), "password_env": f"SDTF_RFC_DEST_{s.sid.upper()}_PASSWD", "keys": list(RFC_DEST_KEYS)}
    if s.connector == "API":
        out["api"] = {"transport": api.get("transport") or "https", "dest": tapi.mask_api_destination(dict(api.get("dest") or {})), "resolved": tapi.mask_api_destination(tapi.resolve_api_destination(s.sid, s.meta)), "password_env": f"SDTF_S4_API_{s.sid.upper()}_PASSWD", "keys": list(API_DEST_KEYS)}
    return out


@router.get("/systems/{system_id}/destination", tags=["systems"])
def get_destination(s: SapSystem = Depends(get_system), p: Principal = Depends(require("project:read"))):
    """The connection parameters of a system with secrets masked, as stored and as resolved from the environment."""
    return _destination_out(s)


@router.put("/systems/{system_id}/destination", tags=["systems"])
def put_destination(req: DestinationIn, kind: str = Query("rfc", pattern="^(rfc|api)$"), s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Set the RFC destination (`kind=rfc`) or the API destination (`kind=api`) of a system and optionally its
    transport. Secrets are refused: a password is referenced as 'env:NAME' or set through the environment
    variable the response names. Only the known connection keys are stored; the read configuration and the
    other metadata stay untouched. Audited."""
    from ..runtime.rfc import SECRET_KEYS
    from ..runtime.target_api import SECRET_KEYS as API_SECRET_KEYS

    if kind == "api" and s.connector != "API":
        raise HTTPException(409, "the API destination is only defined for API targets")
    if kind == "rfc" and s.connector not in ("RFC", "API"):
        raise HTTPException(409, "the RFC destination is only defined for RFC sources and API targets hosting the read-only add-on")
    secrets = SECRET_KEYS if kind == "rfc" else API_SECRET_KEYS
    allowed = RFC_DEST_KEYS if kind == "rfc" else API_DEST_KEYS
    dest = {}
    for k, v in (req.dest or {}).items():
        k = str(k).lower()
        if k in secrets:
            if isinstance(v, str) and v.startswith("env:") and len(v) > 4:
                dest[k] = v
                continue
            raise HTTPException(400, f"{k} is never stored: reference it as 'env:NAME' or set the environment variable the destination document names")
        if k not in allowed:
            raise HTTPException(400, f"unknown connection key {k}; allowed: {', '.join(allowed)}")
        if v is None or v == "":
            continue
        dest[k] = v if isinstance(v, (bool, int)) else str(v).strip()
    transports = ("simulated", "pyrfc") if kind == "rfc" else ("simulated", "https")
    if req.transport is not None and req.transport not in transports:
        raise HTTPException(400, f"transport must be one of {', '.join(transports)}")
    meta = dict(s.meta or {})
    section = dict(meta.get(kind) or {})
    section["dest"] = dest
    if req.transport is not None:
        section["transport"] = req.transport
    meta[kind] = section
    s.meta = meta
    s.connector_status = _connector_status(s.connector, meta)
    db.flush()
    record_event(db, p.username, "DESTINATION_CHANGED", "SYSTEM", s.id, {"kind": kind, "transport": section.get("transport"), "keys": sorted(dest)})
    return _destination_out(s)


class SimulateChanges(BaseModel):
    seed: int = 1
    count: int = Field(10, ge=1, le=500)
    company_codes: list[str] | None = None


@router.post("/systems/{system_id}/simulate-changes", tags=["systems"])
def simulate_changes(req: SimulateChanges, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Play business activity on a simulated source (documents created/changed/deleted after the initial
    extraction) and write its change log, which the simulated add-on serves through Z_SDTF_CDC_POLL. Respects a
    declared business freeze. Not available for real SAP systems."""
    from ..runtime.activity import simulate_business_activity
    from ..runtime.delta import frozen_company_codes

    if not _uses_record_store(s) or s.role != "SOURCE":
        raise HTTPException(409, "business activity can only be simulated on a SYNTHETIC or simulated-RFC source system")
    out = simulate_business_activity(db, s, seed=req.seed, count=req.count, company_codes=req.company_codes, frozen_ccs=frozen_company_codes(db, s.project_id), actor=p.username)
    record_event(db, p.username, "SOURCE_ACTIVITY_SIMULATED", "SYSTEM", s.id, {k: v for k, v in out.items() if k != "by_kind"})
    return out


class SyntheticImport(BaseModel):
    scale: int = Field(1, ge=1, le=10)
    seed: int = 42


@router.post("/systems/{system_id}/import-synthetic", tags=["systems"])
def import_synthetic(req: SyntheticImport, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    if not _uses_record_store(s):
        raise HTTPException(409, "synthetic import is only possible for SYNTHETIC systems or RFC systems on the simulated add-on")
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
def discovery_path(s: SapSystem) -> str:
    """rfc: through the read-only add-on (every RFC source, simulated or live); record_store: the platform's copy
    (synthetic systems, API targets)."""
    return "rfc" if s.connector == "RFC" else "record_store"


@router.post("/systems/{system_id}/discover", tags=["discovery"])
def run_discovery(path: str | None = Query(None, pattern="^(rfc|record_store)$", description="override the read path: rfc (through the add-on) or record_store"), sample: int | None = Query(None, ge=10, le=10000, description="rfc path: read only the first N instances per business object (quick look; the scope engine refuses a sampled discovery)"), s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    from ..discovery.rfc_discovery import discover_over_rfc

    chosen = path or discovery_path(s)
    if chosen == "rfc" and s.connector not in ("RFC", "API", "SYNTHETIC"):
        raise HTTPException(409, f"no RFC path for connector {s.connector}")
    snap = discover_over_rfc(db, s, p.username, sample=sample) if chosen == "rfc" else discover_system(db, s, p.username)
    if snap.status == "FAILED":
        raise HTTPException(502, f"discovery through the add-on failed: {snap.summary.get('read', {}).get('error', '')}")
    record_event(db, p.username, "DISCOVERY_COMPLETED", "SYSTEM", s.id, {"snapshot": snap.id, "path": chosen})
    return {"snapshot_id": snap.id, "path": chosen, "summary": snap.summary}


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


@router.get("/systems/{system_id}/process-analysis", tags=["discovery"])
def process_analysis(area: str = Query("ALL", pattern="^(ALL|O2C|P2P|R2R|all|o2c|p2p|r2r)$"), bukrs: str | None = Query(None, description="comma-separated company codes for the pushdown"), top: int = Query(10, ge=1, le=100), retention_years: int = Query(7, ge=0, le=50), markdown: bool = False, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """Business process analysis from the database footprint through the add-on's aggregate module: process
    variants (TAANA), selectivity (DB05), age profiles, growth (DB02) and table-to-object links (DB15); workload
    statistics are reported as not available."""
    from ..discovery import process_analysis as pa

    res = pa.analyse(db, s, area, [c.strip() for c in bukrs.split(",")] if bukrs else None, top, retention_years)
    if markdown:
        res["markdown"] = pa.report_markdown(res)
    return res


class WorkloadImport(BaseModel):
    text: str = Field(..., max_length=20_000_000, description="the ST03N transaction-profile export (delimited text with a header)")
    period: str = Field("", max_length=60, description="the period the profile covers, e.g. 2026-09 or 'last 3 months'")
    source_file: str = Field("", max_length=120)


@router.post("/systems/{system_id}/workload/import", tags=["discovery"])
def workload_import(req: WorkloadImport, s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:write"))):
    """Import an ST03N transaction-profile export for the system: workload statistics are not table reads, so
    they come from the export a Basis administrator makes, never from a guess."""
    from ..discovery.workload import import_workload

    try:
        w = import_workload(db, s, req.text, req.period, p.username, req.source_file)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {k: v for k, v in w.items() if k != "rows"}


@router.get("/systems/{system_id}/workload", tags=["discovery"])
def workload_get(s: SapSystem = Depends(get_system), db: Session = Depends(get_db), p: Principal = Depends(require("project:read"))):
    """The imported workload compared with the footprint (variants from the record store / add-on are not re-read
    here: the comparison uses the discovery statistics for the documents in the database)."""
    from ..discovery.workload import table_rows, usage_section

    u = usage_section(s, None, table_rows(db, s))
    if u is None:
        return {"status": "NOT_AVAILABLE", "reason": "no ST03N transaction profile imported for this system"}
    return u


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

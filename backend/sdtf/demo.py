"""Programmatic vertical slice: synthetic ECC landscape -> discovery -> graph -> carve-out scope -> manifest
-> rules -> simulated migration -> reconciliation -> auditable report. Used by the CLI, tests and seeding."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .audit.service import record_event
from .catalog.store import RecordStore, import_tables
from .discovery.service import discover_system
from .graph.service import build_graph, persist_graph
from .models import Project, RuleSet, SapSystem
from .rules.engine import parse_ruleset, validate_ruleset
from .rules.factory import generate_candidate_ruleset
from .runtime.pipeline import start_run
from .scope.models import ScopeDefinition, TargetOwnership
from .scope.service import apply_disposition, approve_manifest, create_manifest, pending_dispositions
from .synthetic.ecc_generator import LandscapeSpec, generate_landscape

DEMO_SPINCO = "5000"


def create_demo_project(session: Session, actor: str = "architect", scale: int = 1, seed: int = 42, name: str = "Project Aurora - Specialty Materials carve-out") -> dict:
    project = Project(name=name, scenario_type="CARVE_OUT", description="Divestiture of Nordlicht Specialty Materials GmbH (company code 5000) into an independent S/4HANA landscape", created_by=actor, meta={"reference_scenario": "ECC 6.0 EHP8 -> S/4HANA 2025 SpinCo", "scale": scale, "seed": seed})
    session.add(project)
    session.flush()
    src = SapSystem(project_id=project.id, sid="ECP", client="100", role="SOURCE", product="ECC", release="6.0 EHP8", database="Oracle 19c (synthetic)", os_name="Linux (synthetic)", connector="SYNTHETIC", connector_status="SIMULATED", logical_system="ECPCLNT100", meta={"seed": seed, "scale": scale})
    tgt = SapSystem(project_id=project.id, sid="S4P", client="100", role="TARGET", product="S4HANA", release="2025", database="SAP HANA (synthetic)", os_name="Linux (synthetic)", connector="SYNTHETIC", connector_status="SIMULATED", logical_system="S4PCLNT100", meta={"deployment": "private cloud (synthetic)"})
    session.add_all([src, tgt])
    session.flush()
    tables = generate_landscape(LandscapeSpec(seed=seed, scale=scale))
    counts = import_tables(session, src.id, tables)
    # the target is a prepared S/4HANA shell: organisational configuration for SpinCo already exists
    shell = spinco_shell()
    import_tables(session, tgt.id, shell)
    record_event(session, actor, "PROJECT_CREATED", "PROJECT", project.id, {"source": src.id, "target": tgt.id, "rows_imported": sum(counts.values())})
    return {"project": project, "source": src, "target": tgt, "import_counts": counts}


def spinco_shell(bukrs: str = "SP01", plants: tuple[str, ...] = ("SP10", "SP20"), name: str = "Specialty Materials SpinCo GmbH", country: str = "DE", currency: str = "EUR") -> dict[str, list[dict]]:
    """Organisational configuration of a prepared S/4HANA target shell (default: SpinCo)."""
    return {
        "T001": [{"BUKRS": bukrs, "BUTXT": name, "LAND1": country, "WAERS": currency, "KTOPL": "INT", "PERIV": "K4", "SPRAS": "E"}],
        "T001W": [{"WERKS": p, "NAME1": f"Plant {p} ({country})", "BWKEY": p, "LAND1": country, "VKORG": bukrs, "EKORG": bukrs} for p in plants],
        "T001K": [{"BWKEY": p, "BUKRS": bukrs} for p in plants],
        "TKA01": [{"KOKRS": bukrs, "BEZEI": f"Controlling area {name}", "WAERS": currency, "KTOPL": "INT"}],
        "TKA02": [{"BUKRS": bukrs, "KOKRS": bukrs}],
        "TVKO": [{"VKORG": bukrs, "BUKRS": bukrs, "VTEXT": f"Sales org {name}"}],
        "T024E": [{"EKORG": bukrs, "BUKRS": bukrs, "EKOTX": f"Purch org {name}"}],
        "T004": [{"KTOPL": "INT", "KTPLT": "Group chart of accounts"}],
    }


def create_target_shell(session: Session, project: Project, sid: str, source: SapSystem | None = None, keep_source_org: bool = False, shell: dict[str, list[dict]] | None = None) -> SapSystem:
    """Register an additional prepared S/4HANA target. With keep_source_org the source organisational
    configuration is copied as well (scenarios that keep company codes unchanged, e.g. reverse carve-outs)."""
    tgt = SapSystem(project_id=project.id, sid=sid, client="100", role="TARGET", product="S4HANA", release="2025", connector="SYNTHETIC", connector_status="SIMULATED", logical_system=f"{sid}CLNT100")
    session.add(tgt)
    session.flush()
    shell = shell or spinco_shell()
    if keep_source_org and source is not None:
        store = RecordStore.load(session, source.id, tables=list(shell))
        for t in shell:
            shell[t] = shell[t] + list(store.rows(t))
    import_tables(session, tgt.id, shell)
    return tgt


def demo_scope_definition(src: SapSystem, tgt: SapSystem, name: str = "SpinCo 5000 forward carve-out", **overrides) -> ScopeDefinition:
    base = dict(
        name=name,
        description="Forward carve-out of company code 5000 incl. full history; shared masters duplicated; cross-company documents flagged",
        scenario_type="CARVE_OUT",
        source_system_id=src.id,
        target_system_id=tgt.id,
        company_codes=[DEMO_SPINCO],
        shared_object_policy="DUPLICATE",
        cross_company_policy="INCLUDE_FLAG",
        target_ownership=TargetOwnership(company_code_map={"5000": "SP01"}, plant_map={"5010": "SP10", "5020": "SP20"}, controlling_area_map={"1000": "SP01"}),
    )
    base.update(overrides)
    return ScopeDefinition(**base)


def run_vertical_slice(session: Session, scale: int = 1, seed: int = 42, creator: str = "architect", approver: str = "approver") -> dict:
    ctx = create_demo_project(session, creator, scale=scale, seed=seed)
    project, src, tgt = ctx["project"], ctx["source"], ctx["target"]
    store = RecordStore.load(session, src.id)
    snap = discover_system(session, src, creator, store)
    record_event(session, creator, "DISCOVERY_COMPLETED", "SYSTEM", src.id, {"snapshot": snap.id})
    g = build_graph(store, src.id)
    gstats = persist_graph(session, src.id, g)
    record_event(session, creator, "GRAPH_BUILT", "SYSTEM", src.id, gstats)
    defn = demo_scope_definition(src, tgt)
    manifest = create_manifest(session, project.id, defn, creator)
    record_event(session, creator, "MANIFEST_CREATED", "MANIFEST", manifest.id, {"version": manifest.version, "hash": manifest.content_hash})
    # explicit business disposition of ambiguous objects (cross-company documents, export-controlled materials)
    pending = pending_dispositions(manifest)
    cross = [n for n in pending if manifest.selection["classification"][n]["type"] in ("FI.AccountingDocument", "SD.SalesOrder", "SD.Delivery", "SD.BillingDocument", "MM.PurchaseOrder", "MM.MaterialDocument")]
    apply_disposition(session, manifest, cross, "TRANSFER", approver, "SpinCo side of cross-company documents transfers; ParentCo side retained")
    rest = [n for n in pending_dispositions(manifest)]
    apply_disposition(session, manifest, rest, "TRANSFER", approver, "Compliance review completed: export-controlled materials cleared for transfer within EU")
    record_event(session, approver, "MANIFEST_DISPOSITIONED", "MANIFEST", manifest.id, {"cross_company": len(cross), "other": len(rest)})
    approve_manifest(session, manifest, approver)
    yaml_src = generate_candidate_ruleset(defn, "S4HANA")
    rs = parse_ruleset(yaml_src)
    validation = validate_ruleset(rs)
    assert validation["ok"], validation
    ruleset = RuleSet(project_id=project.id, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=yaml_src, compiled={"rules": rs.rules, "lookups": rs.lookups}, validation=validation, status="DRAFT", created_by=creator)
    session.add(ruleset)
    session.flush()
    approve_ruleset(session, ruleset, approver)
    run = start_run(session, project.id, manifest.id, ruleset.id, creator)
    return {"project": project, "source": src, "target": tgt, "snapshot": snap, "graph_stats": gstats, "manifest": manifest, "ruleset": ruleset, "run": run}


def approve_ruleset(session: Session, rs: RuleSet, approver: str, comment: str = "") -> RuleSet:
    if approver == rs.created_by:
        raise PermissionError("four-eyes principle: the creator of a ruleset cannot approve it")
    if not rs.validation.get("ok"):
        raise ValueError("ruleset has validation errors")
    if rs.status != "DRAFT":
        raise ValueError(f"ruleset is {rs.status}")
    from .models import ApprovalRecord

    rs.status = "APPROVED"
    rs.approved_by = approver
    session.add(ApprovalRecord(subject_type="RULESET", subject_id=rs.id, decision="APPROVED", decided_by=approver, kind="TECHNICAL", comment=comment))
    record_event(session, approver, "RULESET_APPROVED", "RULESET", rs.id, {"version": rs.version, "hash": rs.content_hash})
    session.flush()
    return rs

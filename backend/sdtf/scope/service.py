"""Scope evaluation: seeds from organisational filters, semantic expansion through the dependency graph,
carve-out classification, impact statistics, and immutable manifest creation."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..catalog.tables import TABLES
from ..graph.service import Graph, TraversalPolicy, load_graph, split_node, traverse
from ..models import BusinessObjectInstance, SapSystem, ScopeManifest
from .models import ScopeDefinition


def _bo_index(session: Session, system_id: str) -> dict[str, BusinessObjectInstance]:
    rows = session.execute(select(BusinessObjectInstance).where(BusinessObjectInstance.system_id == system_id)).scalars()
    return {f"{r.object_type}:{r.object_key}": r for r in rows}


def select_seeds(defn: ScopeDefinition, g: Graph, idx: dict[str, BusinessObjectInstance]) -> tuple[list[str], list[dict]]:
    seeds, skipped = [], []
    scope_ccs = set(defn.company_codes)
    for nid, node in g.nodes.items():
        t = node["type"]
        bo = BUSINESS_OBJECTS.get(t)
        if bo is None or node["attributes"].get("missing"):
            continue
        if defn.object_types_include and t not in defn.object_types_include:
            continue
        if t in defn.object_types_exclude:
            continue
        inst = idx.get(nid)
        ccs = node["attributes"].get("company_codes") or []
        owner = ccs[0] if ccs else None
        if bo.kind == "CONFIG":
            if t == "CFG.ControllingArea":
                continue  # reached by reference from company code
            if owner in scope_ccs or (t == "CFG.CompanyCode" and split_node(nid)[1] in scope_ccs):
                seeds.append(nid)
            continue
        if owner not in scope_ccs:
            continue
        if bo.kind == "MASTER" and bo.org_scope == "CLIENT":
            # client-level master data is pulled in through references, not org filters, unless it has a cc view in scope
            if not scope_ccs.intersection(ccs):
                continue
        if defn.plants and bo.org_scope == "PLANT" and inst and inst.werks not in defn.plants:
            skipped.append({"node": nid, "reason": "plant filter"})
            continue
        if inst and bo.kind == "TRANSACTIONAL":
            if defn.fiscal_year_from and inst.gjahr and inst.gjahr < defn.fiscal_year_from:
                skipped.append({"node": nid, "reason": f"fiscal year {inst.gjahr} before {defn.fiscal_year_from}"})
                continue
            if defn.fiscal_year_to and inst.gjahr and inst.gjahr > defn.fiscal_year_to:
                skipped.append({"node": nid, "reason": f"fiscal year {inst.gjahr} after {defn.fiscal_year_to}"})
                continue
            if defn.document_status == "OPEN_ONLY" and inst.status != "OPEN":
                skipped.append({"node": nid, "reason": "closed document excluded by document_status=OPEN_ONLY"})
                continue
            if defn.document_status == "CLOSED_ONLY" and inst.status != "CLOSED":
                skipped.append({"node": nid, "reason": "open document excluded by document_status=CLOSED_ONLY"})
                continue
        seeds.append(nid)
    return seeds, skipped


def classify(defn: ScopeDefinition, g: Graph, idx: dict[str, BusinessObjectInstance], included: dict[str, str]) -> dict[str, dict]:
    scope_ccs = set(defn.company_codes)
    out: dict[str, dict] = {}
    export_controlled = {split_node(n)[1] for n in g.nodes if n.startswith("Z.ExportControl:")}
    for nid, inclusion in included.items():
        node = g.nodes[nid]
        t = node["type"]
        bo = BUSINESS_OBJECTS.get(t)
        ccs = node["attributes"].get("company_codes") or []
        inside = scope_ccs.intersection(ccs)
        outside = set(ccs) - scope_ccs
        inst = idx.get(nid)
        cls, reason, approval = "FULLY_TRANSFERRED", "", False
        if bo is None:
            cls, reason = "EXCLUDED", "Unknown object type"
        elif bo.kind == "TECHNICAL":
            cls, reason, approval = "MANUAL_DISPOSITION", "Technical/interface object requires explicit disposition", True
        elif bo.kind == "CONFIG":
            if t == "CFG.ControllingArea":
                cls, reason = ("SHARED_DUPLICATED", "Controlling area shared with retained company codes; re-created in target") if outside else ("FULLY_TRANSFERRED", "Controlling area used only by carved-out company codes")
            elif inside or split_node(nid)[1] in scope_ccs:
                cls, reason = "FULLY_TRANSFERRED", "Organisational unit belongs to carved-out company code"
            else:
                cls, reason = "REFERENCE_ONLY", "Organisational unit of retained company code, referenced by in-scope documents"
        elif bo.kind == "MASTER":
            if t == "MD.Material" and split_node(nid)[1] in export_controlled:
                cls, reason, approval = "MANUAL_DISPOSITION", "Export-controlled material requires compliance review before transfer", True
            elif inside and outside:
                cls = {"DUPLICATE": "SHARED_DUPLICATED", "REFERENCE": "REFERENCE_ONLY", "EXCLUDE": "EXCLUDED", "MANUAL": "MANUAL_DISPOSITION"}[defn.shared_object_policy]
                reason = f"Shared master data (views in {sorted(ccs)}); policy={defn.shared_object_policy}"
                approval = defn.shared_object_policy == "MANUAL"
            elif inside:
                cls, reason = "FULLY_TRANSFERRED", "Master data owned exclusively by carved-out company codes"
            elif ccs:
                cls, reason = "REFERENCE_ONLY", "Master data of retained company code referenced by in-scope documents"
            else:
                cls, reason = ("FULLY_TRANSFERRED", "Client-level master data reached through in-scope reference") if inclusion == "FULL" else ("REFERENCE_ONLY", "Client-level master data referenced only")
        else:  # TRANSACTIONAL
            owner = ccs[0] if ccs else None
            year = inst.gjahr if inst else None
            if owner is not None and owner not in scope_ccs:
                cls, reason = "RETAINED_BY_SELLER", f"Counterpart / related document owned by retained company code {owner}; linked from in-scope documents"
            elif year and ((defn.fiscal_year_from and year < defn.fiscal_year_from) or (defn.fiscal_year_to and year > defn.fiscal_year_to)):
                cls, reason = "RETAINED_BY_SELLER", f"Related document of fiscal year {year} outside the scoped range; retained as history (balance carry-forward required)"
            elif inside and outside:
                cls = {"INCLUDE_FLAG": "PARTIALLY_TRANSFERRED", "REFERENCE": "REFERENCE_ONLY", "EXCLUDE": "EXCLUDED"}[defn.cross_company_policy]
                reason = f"Cross-company document touching {sorted(ccs)}; policy={defn.cross_company_policy}"
                approval = defn.cross_company_policy == "INCLUDE_FLAG"
            elif inside:
                if defn.historical_policy == "OPEN_ITEMS_AND_BALANCES" and inst and inst.status == "CLOSED":
                    cls, reason = "RETAINED_BY_SELLER", "Closed document retained as history (policy OPEN_ITEMS_AND_BALANCES); balances carried forward"
                else:
                    cls, reason = "FULLY_TRANSFERRED", "Document owned by carved-out company code"
            else:
                cls, reason = "RETAINED_BY_SELLER", "Counterpart / related document owned by retained company code"
        if inclusion == "REFERENCE" and cls in ("FULLY_TRANSFERRED", "SHARED_DUPLICATED", "PARTIALLY_TRANSFERRED"):
            cls, reason = "REFERENCE_ONLY", reason + " (reached by reference-only policy)"
        if inclusion == "FLAGGED" and cls == "FULLY_TRANSFERRED":
            cls, approval = "MANUAL_DISPOSITION", True
            reason = reason + " (flagged by traversal policy)"
        out[nid] = {"type": t, "classification": cls, "reason": reason, "requires_approval": approval, "company_codes": ccs, "inclusion": inclusion, "status": inst.status if inst else None, "gjahr": inst.gjahr if inst else None}
    return out


def impact_of(classification: dict[str, dict], traces: list[dict], stopped: list[dict], skipped: list[dict], missing: list[str]) -> dict:
    by_type: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    est_bytes = 0
    open_docs = 0
    for nid, c in classification.items():
        by_type[c["type"]][c["classification"]] += 1
        totals[c["classification"]] += 1
        bo = BUSINESS_OBJECTS.get(c["type"])
        if bo and c["classification"] in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED"):
            est_bytes += TABLES[bo.header_table].avg_row_bytes + sum(TABLES[t].avg_row_bytes * 2 for t in bo.item_tables if t in TABLES)
            if c.get("status") == "OPEN":
                open_docs += 1
    approvals = sum(1 for c in classification.values() if c["requires_approval"])
    warnings = []
    if totals["PARTIALLY_TRANSFERRED"]:
        warnings.append(f"{totals['PARTIALLY_TRANSFERRED']} cross-company documents are partially transferred and need business approval")
    if totals["MANUAL_DISPOSITION"]:
        warnings.append(f"{totals['MANUAL_DISPOSITION']} objects require manual disposition")
    if missing:
        warnings.append(f"{len(missing)} referenced objects are missing in the source (dangling references)")
    # excluded objects that transferred documents still point at: referential integrity will fail in the target
    transferred = {n for n, c in classification.items() if c["classification"] in ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")}
    excluded_referenced = sorted({t["node"] for t in traces if t.get("from") in transferred and classification.get(t["node"], {}).get("classification") == "EXCLUDED"})
    if excluded_referenced:
        warnings.append(f"{len(excluded_referenced)} excluded objects are referenced by transferred documents; referential integrity checks will fail unless they are duplicated or referenced")
    expanded = sum(1 for t in traces if t["policy"] != "SEED")
    return {
        "objects_total": len(classification),
        "objects_seeded": sum(1 for t in traces if t["policy"] == "SEED"),
        "objects_expanded": expanded,
        "objects_stopped": len(stopped),
        "objects_skipped_by_filters": len(skipped),
        "by_classification": dict(totals),
        "by_type": {t: dict(c) for t, c in by_type.items()},
        "est_bytes": est_bytes,
        "open_documents": open_docs,
        "approvals_required": approvals,
        "missing_references": len(missing),
        "excluded_but_referenced": len(excluded_referenced),
        "warnings": warnings,
    }


def evaluate_scope(session: Session, defn: ScopeDefinition) -> dict:
    g = load_graph(session, defn.source_system_id)
    if not g.nodes:
        raise ValueError("Dependency graph has not been built for the source system")
    idx = _bo_index(session, defn.source_system_id)
    seeds, skipped = select_seeds(defn, g, idx)
    pol = TraversalPolicy()
    pol.edge_policies.update(defn.edge_policies)
    pol.type_policies.update(defn.type_policies)
    if defn.shared_object_policy == "EXCLUDE":
        pass  # handled in classification (shared masters become EXCLUDED but are still discovered for reporting)
    res = traverse(g, seeds, pol)
    classification = classify(defn, g, idx, res.included)
    impact = impact_of(classification, res.traces, res.stopped, skipped, res.missing)
    return {"definition": defn.model_dump(), "impact": impact, "classification": classification, "traces": res.traces, "stopped": res.stopped, "skipped": skipped, "missing": res.missing}


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def create_manifest(session: Session, project_id: str, defn: ScopeDefinition, actor: str, evaluation: dict | None = None) -> ScopeManifest:
    for sid in (defn.source_system_id, defn.target_system_id):
        sysrow = session.get(SapSystem, sid)
        if sysrow is None or sysrow.project_id != project_id:
            raise ValueError(f"System {sid} does not belong to project {project_id}")
    ev = evaluation or evaluate_scope(session, defn)
    selection = {"classification": ev["classification"], "traces": ev["traces"], "stopped": ev["stopped"], "skipped": ev["skipped"], "missing": ev["missing"]}
    version = (session.execute(select(func.max(ScopeManifest.version)).where(ScopeManifest.project_id == project_id, ScopeManifest.name == defn.name)).scalar() or 0) + 1
    content_hash = canonical_hash({"definition": defn.model_dump(), "classification": ev["classification"], "version": version})
    m = ScopeManifest(project_id=project_id, name=defn.name, version=version, content_hash=content_hash, definition=defn.model_dump(), impact=ev["impact"], selection=selection, status="DRAFT", created_by=actor)
    session.add(m)
    session.flush()
    return m


def verify_manifest_integrity(m: ScopeManifest) -> bool:
    return canonical_hash({"definition": m.definition, "classification": m.selection.get("classification", {}), "version": m.version}) == m.content_hash


def compare_manifests(a: ScopeManifest, b: ScopeManifest) -> dict:
    ca, cb = a.selection.get("classification", {}), b.selection.get("classification", {})
    only_a = sorted(set(ca) - set(cb))
    only_b = sorted(set(cb) - set(ca))
    changed = sorted(n for n in set(ca) & set(cb) if ca[n]["classification"] != cb[n]["classification"])
    return {
        "a": {"id": a.id, "name": a.name, "version": a.version, "impact": a.impact},
        "b": {"id": b.id, "name": b.name, "version": b.version, "impact": b.impact},
        "only_in_a": len(only_a),
        "only_in_b": len(only_b),
        "reclassified": len(changed),
        "samples": {"only_in_a": only_a[:25], "only_in_b": only_b[:25], "reclassified": [{"node": n, "a": ca[n]["classification"], "b": cb[n]["classification"]} for n in changed[:25]]},
        "delta_bytes": b.impact.get("est_bytes", 0) - a.impact.get("est_bytes", 0),
        "delta_objects": b.impact.get("objects_total", 0) - a.impact.get("objects_total", 0),
    }


# ------------------------------------------------------------------------------ dispositions / approval
DISPOSITION_TO_CLASS = {"TRANSFER": "FULLY_TRANSFERRED", "TRANSFER_PARTIAL": "PARTIALLY_TRANSFERRED", "RETAIN": "RETAINED_BY_SELLER", "DUPLICATE": "SHARED_DUPLICATED", "REFERENCE": "REFERENCE_ONLY", "EXCLUDE": "EXCLUDED"}


def pending_dispositions(m: ScopeManifest) -> list[str]:
    return [n for n, c in m.selection.get("classification", {}).items() if c.get("requires_approval")]


def apply_disposition(session: Session, m: ScopeManifest, nodes: list[str], decision: str, actor: str, comment: str = "") -> int:
    """Business disposition of ambiguous objects. Only DRAFT manifests can be amended; the content hash is
    recomputed and the decision is recorded on every affected node."""
    if m.status != "DRAFT":
        raise ValueError(f"manifest is {m.status}; dispositions are only accepted on DRAFT manifests")
    if decision not in DISPOSITION_TO_CLASS:
        raise ValueError(f"unknown disposition {decision}")
    cls = dict(m.selection.get("classification", {}))
    n = 0
    for node in nodes:
        c = cls.get(node)
        if c is None:
            continue
        new_cls = DISPOSITION_TO_CLASS[decision]
        if decision == "TRANSFER" and len(c.get("company_codes") or []) > 1 and BUSINESS_OBJECTS.get(c["type"]) and BUSINESS_OBJECTS[c["type"]].kind == "TRANSACTIONAL":
            new_cls = "PARTIALLY_TRANSFERRED"
        cls[node] = {**c, "classification": new_cls, "requires_approval": False, "disposition": {"decision": decision, "by": actor, "comment": comment, "previous": c["classification"]}}
        n += 1
    sel = dict(m.selection)
    sel["classification"] = cls
    m.selection = sel
    m.impact = impact_of(cls, sel.get("traces", []), sel.get("stopped", []), sel.get("skipped", []), sel.get("missing", []))
    m.content_hash = canonical_hash({"definition": m.definition, "classification": cls, "version": m.version})
    session.flush()
    return n


def approve_manifest(session: Session, m: ScopeManifest, approver: str, comment: str = "") -> ScopeManifest:
    from ..audit.service import record_event
    from ..models import ApprovalRecord, utcnow

    if approver == m.created_by:
        raise PermissionError("four-eyes principle: the creator of a manifest cannot approve it")
    if m.status != "DRAFT":
        raise ValueError(f"manifest is {m.status}")
    pending = pending_dispositions(m)
    if pending:
        raise ValueError(f"{len(pending)} object(s) still require business disposition before approval")
    if not verify_manifest_integrity(m):
        raise ValueError("manifest content hash mismatch")
    m.status = "APPROVED"
    m.approved_by = approver
    m.approved_at = utcnow()
    session.add(ApprovalRecord(subject_type="MANIFEST", subject_id=m.id, decision="APPROVED", decided_by=approver, kind="BUSINESS", comment=comment))
    record_event(session, approver, "MANIFEST_APPROVED", "MANIFEST", m.id, {"version": m.version, "hash": m.content_hash})
    session.flush()
    return m


def reject_manifest(session: Session, m: ScopeManifest, approver: str, comment: str = "") -> ScopeManifest:
    from ..audit.service import record_event
    from ..models import ApprovalRecord

    if m.status != "DRAFT":
        raise ValueError(f"manifest is {m.status}")
    m.status = "REJECTED"
    session.add(ApprovalRecord(subject_type="MANIFEST", subject_id=m.id, decision="REJECTED", decided_by=approver, kind="BUSINESS", comment=comment))
    record_event(session, approver, "MANIFEST_REJECTED", "MANIFEST", m.id, {"comment": comment})
    session.flush()
    return m

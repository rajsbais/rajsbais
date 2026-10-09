"""The twelve bounded agents. Each one reads stored evidence and returns a Proposal."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS
from ..discovery.service import latest_snapshot
from ..models import MigrationRun, ReconciliationResult, SapSystem, ScopeManifest, TableStatistic
from ..rules.factory import generate_candidate_ruleset
from ..scope.models import ScopeDefinition
from .framework import Agent, Evidence, Proposal, register


def _systems(session: Session, project_id: str) -> list[SapSystem]:
    return session.execute(select(SapSystem).where(SapSystem.project_id == project_id)).scalars().all()


def _manifest(session: Session, context: dict) -> ScopeManifest:
    m = session.get(ScopeManifest, context.get("manifest_id", ""))
    if m is None:
        raise ValueError("context.manifest_id is required")
    return m


def _run(session: Session, context: dict) -> MigrationRun:
    r = session.get(MigrationRun, context.get("run_id", ""))
    if r is None:
        raise ValueError("context.run_id is required")
    return r


@register
class LandscapeDiscoveryAgent(Agent):
    name = "landscape_discovery"
    description = "Summarises discovered systems and recommends what to discover next"

    def propose(self, session, project_id, context):
        systems = _systems(session, project_id)
        items, ev = [], []
        for s in systems:
            snap = latest_snapshot(session, s.id)
            if snap is None:
                items.append({"system": s.sid, "action": "RUN_DISCOVERY", "reason": "no discovery snapshot"})
            else:
                summ = snap.summary
                items.append({"system": s.sid, "action": "NONE", "tables": summ.get("tables", {}).get("count"), "complexity": summ.get("complexity", {}).get("band")})
                ev.append(Evidence("SNAPSHOT", snap.id, f"{s.sid} discovery"))
        conf = 0.9 if ev else 0.6
        return Proposal("Discovery coverage of project systems", {"items": items}, conf, ev, requires_approval=False)


@register
class BusinessObjectClassificationAgent(Agent):
    name = "business_object_classification"
    description = "Classifies custom (Z/Y) tables into functional domains and migration relevance"

    def propose(self, session, project_id, context):
        out, ev = [], []
        for s in _systems(session, project_id):
            snap = latest_snapshot(session, s.id)
            if snap is None:
                continue
            stats = session.execute(select(TableStatistic).where(TableStatistic.snapshot_id == snap.id, TableStatistic.is_custom.is_(True))).scalars().all()
            for t in stats:
                fields = set(t.fields)
                domain, conf = "UNKNOWN", 0.4
                if "BUKRS" in fields or "SAKNR" in fields:
                    domain, conf = "FI", 0.8
                if "MATNR" in fields:
                    domain, conf = "MM/SD", 0.75
                if "LIFNR" in fields:
                    domain, conf = "MM", 0.8
                if "KUNNR" in fields:
                    domain, conf = "SD", 0.8
                name_hint = t.table_name[1:3]
                if name_hint in ("SD", "FI", "MM", "CO", "PP"):
                    domain, conf = name_hint, min(0.95, conf + 0.15)
                out.append({"table": t.table_name, "domain": domain, "rows": t.row_count, "migration_relevant": t.row_count > 0, "confidence": conf})
                ev.append(Evidence("SNAPSHOT", snap.id, t.table_name))
        return Proposal("Custom table classification", {"tables": out}, sum(o["confidence"] for o in out) / len(out) if out else 0.3, ev)


@register
class ScopeRecommendationAgent(Agent):
    name = "scope_recommendation"
    description = "Recommends a scope definition for a company code based on discovered volumes"

    def propose(self, session, project_id, context):
        cc = context.get("company_code")
        src = next((s for s in _systems(session, project_id) if s.role == "SOURCE"), None)
        tgt = next((s for s in _systems(session, project_id) if s.role == "TARGET"), None)
        if not cc or src is None:
            raise ValueError("context.company_code and a source system are required")
        snap = latest_snapshot(session, src.id)
        inv = snap.summary.get("business_objects", {}) if snap else {}
        years = Counter()
        for bo in inv.values():
            for y, n in bo.get("by_year", {}).items():
                years[y] += n
        total = sum(years.values()) or 1
        recent = sorted(years)[-2:] if years else []
        recent_share = sum(years[y] for y in recent) / total if recent else 0
        historical = "FULL" if total < 5000 else ("YEARS" if recent_share > 0.6 else "OPEN_ITEMS_AND_BALANCES")
        shared = sum(bo.get("shared", 0) for bo in inv.values())
        defn = {"name": f"Carve-out {cc} (recommended)", "scenario_type": "CARVE_OUT", "source_system_id": src.id, "target_system_id": tgt.id if tgt else "", "company_codes": [cc], "historical_policy": historical, "fiscal_year_from": int(recent[0]) if historical == "YEARS" and recent else None, "shared_object_policy": "DUPLICATE" if shared < 50 else "MANUAL", "cross_company_policy": "INCLUDE_FLAG", "target_ownership": {"company_code_map": {cc: cc}}}
        facts = {"objects_total": total, "recent_share": round(recent_share, 2), "shared_master_data": shared}
        return Proposal(f"Recommended scope for company code {cc}: {self.reasoner.explain(facts)}", {"scope_definition": defn, "facts": facts}, 0.7, [Evidence("SNAPSHOT", snap.id if snap else "", "volumes by year")])


@register
class DependencyAnalysisAgent(Agent):
    name = "dependency_analysis"
    description = "Explains the dependency expansion of a manifest and highlights risks"

    def propose(self, session, project_id, context):
        m = _manifest(session, context)
        traces = m.selection.get("traces", [])
        by_edge = Counter(t.get("edge") for t in traces if t.get("edge"))
        depth = Counter(t.get("depth") for t in traces)
        chains = [t for t in traces if t.get("edge") == "CROSS_COMPANY"][:10]
        return Proposal(f"{m.impact.get('objects_expanded', 0)} objects were added by dependency expansion", {"expansion_by_edge": dict(by_edge), "by_depth": dict(depth), "missing_references": m.selection.get("missing", [])[:20], "sample_cross_company_chains": chains}, 0.85, [Evidence("MANIFEST", m.id)], requires_approval=False)


@register
class CarveoutOwnershipAgent(Agent):
    name = "carveout_ownership"
    description = "Proposes dispositions for objects that require manual ownership decisions"

    def propose(self, session, project_id, context):
        m = _manifest(session, context)
        cls = m.selection.get("classification", {})
        scope = set(m.definition["company_codes"])
        props = []
        for nid, c in cls.items():
            if not c.get("requires_approval"):
                continue
            bo = BUSINESS_OBJECTS.get(c["type"])
            if bo and bo.kind == "TRANSACTIONAL":
                decision, conf, why = "TRANSFER", 0.75, "SpinCo side of the cross-company document is needed for SpinCo's books; ParentCo side stays"
            elif "Export-controlled" in c["reason"]:
                decision, conf, why = "RETAIN", 0.55, "Export-controlled material: default to retain until compliance clears it"
            elif bo and bo.kind == "MASTER":
                inside = len(scope.intersection(c["company_codes"]))
                decision, conf, why = ("DUPLICATE", 0.7, "Shared master data with SpinCo view: duplicate general data") if inside else ("REFERENCE", 0.6, "No SpinCo view: reference only")
            else:
                decision, conf, why = "REFERENCE", 0.5, "Technical object: review interfaces manually"
            props.append({"node": nid, "decision": decision, "confidence": conf, "rationale": why})
        avg = sum(p["confidence"] for p in props) / len(props) if props else 0.9
        return Proposal(f"{len(props)} disposition proposal(s)", {"dispositions": props}, avg, [Evidence("MANIFEST", m.id)], subject_type="MANIFEST", subject_id=m.id)


@register
class TransformationMappingAgent(Agent):
    name = "transformation_mapping"
    description = "Generates a candidate transformation ruleset from the scope and target metadata (Rule Factory)"

    def propose(self, session, project_id, context):
        m = _manifest(session, context)
        defn = ScopeDefinition(**m.definition)
        tgt = session.get(SapSystem, defn.target_system_id)
        yaml_src = generate_candidate_ruleset(defn, tgt.product if tgt else "S4HANA")
        return Proposal("Candidate ruleset generated from scope and target release", {"ruleset_yaml": yaml_src, "target_product": tgt.product if tgt else "S4HANA"}, 0.65, [Evidence("MANIFEST", m.id), Evidence("GRAPH", defn.source_system_id, "S/4 compatibility registry")], subject_type="MANIFEST", subject_id=m.id)


@register
class DataQualityAgent(Agent):
    name = "data_quality"
    description = "Finds source data quality issues relevant to migration"

    def propose(self, session, project_id, context):
        from ..catalog.store import RecordStore

        src = next((s for s in _systems(session, project_id) if s.role == "SOURCE"), None)
        store = RecordStore.load(session, src.id, tables=["KNA1", "KNB1", "LFA1", "LFB1", "MARA", "MARC", "VBAK", "EKKO"])
        issues = []
        cust_with_view = {r["KUNNR"] for r in store.rows("KNB1")}
        no_view = [r["KUNNR"] for r in store.rows("KNA1") if r["KUNNR"] not in cust_with_view]
        if no_view:
            issues.append({"check": "customer_without_company_view", "count": len(no_view), "samples": no_view[:5]})
        vend_with_view = {r["LIFNR"] for r in store.rows("LFB1")}
        no_vview = [r["LIFNR"] for r in store.rows("LFA1") if r["LIFNR"] not in vend_with_view]
        if no_vview:
            issues.append({"check": "vendor_without_company_view", "count": len(no_vview)})
        mat_with_plant = {r["MATNR"] for r in store.rows("MARC")}
        no_plant = [r["MATNR"] for r in store.rows("MARA") if r["MATNR"] not in mat_with_plant]
        if no_plant:
            issues.append({"check": "material_without_plant_view", "count": len(no_plant), "samples": no_plant[:5]})
        names = Counter(r["NAME1"].strip().lower() for r in store.rows("KNA1"))
        dups = [n for n, c in names.items() if c > 1]
        if dups:
            issues.append({"check": "potential_duplicate_customers", "count": len(dups), "samples": dups[:5]})
        kunnr = {r["KUNNR"] for r in store.rows("KNA1")}
        dangling = [r["VBELN"] for r in store.rows("VBAK") if r["KUNNR"] not in kunnr]
        if dangling:
            issues.append({"check": "sales_order_dangling_customer", "count": len(dangling)})
        return Proposal(f"{len(issues)} data quality issue type(s) detected", {"issues": issues, "score": max(0, 100 - 10 * len(issues))}, 0.8, [Evidence("SNAPSHOT", src.id, "source record store")], requires_approval=False)


@register
class MigrationPerformanceAgent(Agent):
    name = "migration_performance"
    description = "Models throughput and predicts whether the migration fits the downtime window"

    def propose(self, session, project_id, context):
        runs = session.execute(select(MigrationRun).where(MigrationRun.project_id == project_id, MigrationRun.status == "COMPLETED")).scalars().all()
        window_h = float(context.get("downtime_window_hours", 24))
        measured = []
        for r in runs:
            for st in r.stages:
                if st.name == "EXTRACT" and st.metrics.get("records_per_second"):
                    measured.append(st.metrics["records_per_second"])
        rps = (sum(measured) / len(measured)) if measured else None
        target_records = float(context.get("target_records", 50_000_000))
        if rps:
            # simulated in-memory throughput is not representative of SAP extraction; apply a conservative haircut
            effective = rps * 0.05
            hours = target_records / effective / 3600
            verdict = "FITS" if hours < window_h * 0.6 else ("AT_RISK" if hours < window_h else "DOES_NOT_FIT")
            conf = 0.35
        else:
            effective, hours, verdict, conf = None, None, "UNKNOWN", 0.1
        return Proposal(f"Window prediction: {verdict}", {"measured_simulated_rps": rps, "effective_rps_assumed": effective, "predicted_hours": round(hours, 1) if hours else None, "window_hours": window_h, "runs_considered": len(runs), "caveat": "Measured values come from simulated runs on synthetic data; replace with benchmark results from a real source before relying on this prediction"}, conf, [Evidence("RUN", r.id) for r in runs], requires_approval=False)


@register
class ReconciliationExplanationAgent(Agent):
    name = "reconciliation_explanation"
    description = "Explains non-passing reconciliation checks of a run"

    def propose(self, session, project_id, context):
        run = _run(session, context)
        rows = session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.status != "PASS")).scalars().all()
        expl = []
        for r in rows:
            root = r.explanation or "no explanation recorded"
            cat = "SCOPE_POLICY" if "retained" in root.lower() or "excluded" in root.lower() or "partially" in root.lower() else ("TRANSFORMATION" if "rule" in root.lower() else ("LOAD" if "target" in root.lower() else "UNKNOWN"))
            expl.append({"layer": r.layer, "check": r.check_name, "subject": r.subject, "status": r.status, "root_cause_category": cat, "explanation": root, "recommended_action": {"SCOPE_POLICY": "No action if the policy is intended; otherwise amend the scope", "TRANSFORMATION": "Fix the rule and re-run the dry run", "LOAD": "Inspect load exceptions and re-run the LOAD stage", "UNKNOWN": "Manual investigation"}[cat]})
        conf = 0.8 if all(e["root_cause_category"] != "UNKNOWN" for e in expl) else 0.5
        return Proposal(f"{len(expl)} non-passing check(s) explained", {"explanations": expl}, conf, [Evidence("RECONCILIATION", run.id)], requires_approval=False, subject_type="RUN", subject_id=run.id)


@register
class CutoverRiskAgent(Agent):
    name = "cutover_risk"
    description = "Scores cutover risk from open documents, interfaces, pending approvals and reconciliation state"

    def propose(self, session, project_id, context):
        m = _manifest(session, context)
        runs = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == m.id)).scalars().all()
        last = runs[-1] if runs else None
        src = session.get(SapSystem, m.definition["source_system_id"])
        snap = latest_snapshot(session, src.id)
        interfaces = len(snap.summary.get("interfaces", [])) if snap else 0
        factors = {"open_documents": m.impact.get("open_documents", 0), "pending_approvals": m.impact.get("approvals_required", 0), "interfaces": interfaces, "last_run_status": last.status if last else "NONE", "reconciliation": (last.report or {}).get("reconciliation", {}).get("overall", "NONE") if last else "NONE", "rehearsals_completed": len([r for r in runs if r.status == "COMPLETED"])}
        score = min(100, factors["open_documents"] * 0.2 + factors["pending_approvals"] * 5 + interfaces * 4 + (30 if factors["reconciliation"] in ("FAIL", "NONE") else (10 if factors["reconciliation"] == "WARN" else 0)) + (20 if factors["rehearsals_completed"] == 0 else 0))
        return Proposal(f"Cutover risk score {round(score)}", {"score": round(score), "band": "HIGH" if score > 60 else ("MEDIUM" if score > 30 else "LOW"), "factors": factors, "go_no_go_criteria": [{"criterion": "Reconciliation overall PASS or explained WARN", "met": factors["reconciliation"] in ("PASS", "WARN")}, {"criterion": "No pending business dispositions", "met": factors["pending_approvals"] == 0}, {"criterion": "At least one completed rehearsal", "met": factors["rehearsals_completed"] > 0}, {"criterion": "Interface cut-over plan approved", "met": False, "note": "not tracked in this build"}]}, 0.6, [Evidence("MANIFEST", m.id)] + [Evidence("RUN", r.id) for r in runs], requires_approval=False, subject_type="MANIFEST", subject_id=m.id)


@register
class ComplianceAgent(Agent):
    name = "compliance"
    description = "Flags export-control, cross-border and retention concerns for a manifest"

    def propose(self, session, project_id, context):
        m = _manifest(session, context)
        cls = m.selection.get("classification", {})
        src = session.get(SapSystem, m.definition["source_system_id"])
        from ..catalog.store import RecordStore

        store = RecordStore.load(session, src.id, tables=["T001", "ZFI_TSA_SCOPE"])
        countries = {r["BUKRS"]: r["LAND1"] for r in store.rows("T001")}
        scope = m.definition["company_codes"]
        target_country = context.get("target_country", "DE")
        findings = []
        ec = [n for n, c in cls.items() if "Export-controlled" in c["reason"] or (c.get("disposition") and "export-controlled" in c["disposition"].get("comment", "").lower())]
        if ec:
            findings.append({"type": "EXPORT_CONTROL", "severity": "HIGH", "count": len(ec), "requirement": "Compliance sign-off before any transfer; license check per destination"})
        cross_border = [cc for cc in scope if countries.get(cc) and countries[cc] != target_country]
        if cross_border:
            findings.append({"type": "CROSS_BORDER_TRANSFER", "severity": "MEDIUM", "company_codes": cross_border, "requirement": "Data transfer impact assessment and personal-data masking for non-business-contact fields"})
        tsa = [r for r in store.rows("ZFI_TSA_SCOPE") if r["BUKRS"] in scope]
        if tsa:
            findings.append({"type": "TSA_RETENTION", "severity": "LOW", "count": len(tsa), "requirement": "Retain shared-service data in the source until TSA end dates"})
        personal = [n for n, c in cls.items() if c["type"] in ("MD.Customer", "MD.Vendor") and c["classification"] != "EXCLUDED"]
        findings.append({"type": "PERSONAL_DATA", "severity": "MEDIUM", "count": len(personal), "requirement": "Business partner records may contain personal data: apply masking in non-production targets and document the legal basis"})
        return Proposal(f"{len(findings)} compliance finding(s)", {"findings": findings}, 0.7, [Evidence("MANIFEST", m.id)], subject_type="MANIFEST", subject_id=m.id)


@register
class DocumentationAgent(Agent):
    name = "documentation"
    description = "Drafts a human-readable summary of a manifest or run"

    def propose(self, session, project_id, context):
        parts, ev = [], []
        if context.get("manifest_id"):
            m = _manifest(session, context)
            d = m.definition
            parts.append(f"## Scope manifest {m.name} v{m.version}\n\nScenario: {d['scenario_type']} ({d.get('carve_out_direction', '')}). Company codes: {', '.join(d['company_codes'])}. Historical policy: {d['historical_policy']}. Shared object policy: {d['shared_object_policy']}. Cross-company policy: {d['cross_company_policy']}.\n\nObjects: {m.impact.get('objects_total')} (seeded {m.impact.get('objects_seeded')}, expanded {m.impact.get('objects_expanded')}). Classification: " + ", ".join(f"{k} {v}" for k, v in m.impact.get("by_classification", {}).items()) + f".\n\nStatus: {m.status}" + (f", approved by {m.approved_by}" if m.approved_by else ""))
            ev.append(Evidence("MANIFEST", m.id))
        if context.get("run_id"):
            r = _run(session, context)
            parts.append((r.report or {}).get("markdown", f"Run {r.id}: {r.status}"))
            ev.append(Evidence("RUN", r.id))
        if not parts:
            raise ValueError("context.manifest_id or context.run_id is required")
        return Proposal("Documentation draft", {"markdown": "\n\n".join(parts)}, 0.9, ev, requires_approval=False)


def agent_catalog() -> list[dict]:
    from .framework import REGISTRY

    return [{"name": n, "description": c.description, "permission": c.permission, "forbidden_actions": list(c.forbidden_actions), "reasoner": "heuristic (deterministic)", "status": "IMPLEMENTED"} for n, c in REGISTRY.items()]

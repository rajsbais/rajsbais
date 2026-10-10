"""Scenario matrix: every scope policy combination must run end to end and reconcile with an explained outcome."""
import pytest
from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.demo import approve_ruleset, create_demo_project, create_target_shell, demo_scope_definition
from sdtf.discovery.service import discover_system
from sdtf.graph.service import build_graph, persist_graph
from sdtf.models import ReconciliationResult, RuleSet
from sdtf.rules.engine import parse_ruleset, validate_ruleset
from sdtf.rules.factory import generate_candidate_ruleset
from sdtf.runtime.pipeline import start_run
from sdtf.scope.models import TargetOwnership
from sdtf.scope.service import apply_disposition, approve_manifest, create_manifest, pending_dispositions

SCENARIOS = {
    "years_2024_2025": (dict(fiscal_year_from=2024, fiscal_year_to=2025, historical_policy="YEARS"), "WARN"),
    "open_only": (dict(document_status="OPEN_ONLY"), "WARN"),
    "open_items_balances": (dict(historical_policy="OPEN_ITEMS_AND_BALANCES"), "WARN"),
    "xcc_exclude": (dict(cross_company_policy="EXCLUDE"), "WARN"),
    "xcc_reference": (dict(cross_company_policy="REFERENCE"), "WARN"),
    "shared_reference": (dict(shared_object_policy="REFERENCE"), "WARN"),
    "shared_exclude": (dict(shared_object_policy="EXCLUDE"), "FAIL"),
    "shared_manual": (dict(shared_object_policy="MANUAL"), "PASS"),
    "plant_filter": (dict(plants=["5010"]), "PASS"),
    "two_ccs": (dict(company_codes=["3000", "4000"], target_ownership=TargetOwnership(company_code_map={"3000": "3000", "4000": "4000"})), "PASS"),
    "reverse": (dict(carve_out_direction="REVERSE", company_codes=["1000", "1100", "2000", "3000", "4000"], target_ownership=TargetOwnership()), "PASS"),
}


@pytest.fixture(scope="module")
def landscape(engine):
    from sdtf.db import session_scope

    with session_scope() as s:
        ctx = create_demo_project(s, "architect", scale=1, seed=21, name="scenario matrix")
        store = RecordStore.load(s, ctx["source"].id)
        discover_system(s, ctx["source"], "architect", store)
        persist_graph(s, ctx["source"].id, build_graph(store, ctx["source"].id))
        return {"project_id": ctx["project"].id, "source_id": ctx["source"].id}


def _run_scenario(session, land, name, overrides):
    from sdtf.models import Project, SapSystem

    proj = session.get(Project, land["project_id"])
    src = session.get(SapSystem, land["source_id"])
    keep = name in ("two_ccs", "reverse")
    tgt = create_target_shell(session, proj, f"T{abs(hash(name)) % 1000:03d}", src, keep_source_org=keep)
    defn = demo_scope_definition(src, tgt, name=name, **overrides)
    m = create_manifest(session, proj.id, defn, "architect")
    apply_disposition(session, m, pending_dispositions(m), "TRANSFER", "approver", "test")
    approve_manifest(session, m, "approver")
    y = generate_candidate_ruleset(defn, "S4HANA", name=f"{name}-rules")
    rs = parse_ruleset(y)
    v = validate_ruleset(rs)
    assert v["ok"], v
    row = RuleSet(project_id=proj.id, name=rs.name, version=1, content_hash=rs.content_hash, source_yaml=y, compiled={"rules": rs.rules}, validation=v, created_by="architect")
    session.add(row)
    session.flush()
    approve_ruleset(session, row, "approver")
    return m, start_run(session, proj.id, m.id, row.id, "operator")


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_runs_and_reconciles_with_explanation(session, landscape, name):
    overrides, expected = SCENARIOS[name]
    m, run = _run_scenario(session, landscape, name, overrides)
    assert run.status == "COMPLETED"
    rep = run.report["reconciliation"]
    assert rep["overall"] == expected, (name, rep["failures"][:5])
    assert run.report["exceptions"]["count"] == 0
    # every non-passing financial check must carry an explanation; variances must never be "unexplained"
    for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run.id, ReconciliationResult.layer == "FINANCIAL", ReconciliationResult.status != "PASS")).scalars():
        assert r.explanation, (name, r.check_name, r.subject)
        if r.check_name == "gl_balance":
            assert "unexplained 0.0" in r.explanation or "unexplained -0.0" in r.explanation, (name, r.explanation)
    if name == "shared_exclude":
        assert m.impact["excluded_but_referenced"] > 0 and any("referential integrity" in w for w in m.impact["warnings"])
        assert any(f["check"] in ("business_partner_reference", "material_reference") for f in rep["failures"])
    if name == "open_items_balances":
        assert m.impact["by_classification"]["RETAINED_BY_SELLER"] > m.impact["by_classification"]["FULLY_TRANSFERRED"] / 2
    if name == "reverse":
        assert set(m.definition["company_codes"]) == {"1000", "1100", "2000", "3000", "4000"}
        assert rep["by_layer"]["FINANCIAL"]["PASS"] > 100

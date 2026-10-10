import pytest

from sdtf.agents.catalog import agent_catalog
from sdtf.agents.framework import REGISTRY, decide
from sdtf.carveout.service import completeness_report, residual_exposure_report
from sdtf.cutover.service import generate_runbook
from sdtf.models import ScopeManifest


def test_all_twelve_agents_registered():
    names = {a["name"] for a in agent_catalog()}
    assert names == {"landscape_discovery", "business_object_classification", "scope_recommendation", "dependency_analysis", "carveout_ownership", "transformation_mapping", "data_quality", "migration_performance", "reconciliation_explanation", "cutover_risk", "compliance", "documentation"}


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_agent_proposals_are_bounded(session, slice_result, name):
    ctx = {"manifest_id": slice_result["manifest_id"], "run_id": slice_result["run_id"], "company_code": "5000"}
    d = REGISTRY[name]().run(session, slice_result["project_id"], ctx, "architect")
    assert 0.0 <= d.confidence <= 1.0
    assert d.status == "PROPOSED" and d.proposal["summary"]
    assert set(d.proposal["forbidden_actions"]) == {"authorize_production_migration", "delete_data", "post_financial_adjustment", "change_security_policy"}
    assert all({"kind", "ref"} <= set(e) for e in d.evidence)
    decide(session, d, True, "approver")
    assert d.status == "ACCEPTED" and d.decided_by == "approver"
    with pytest.raises(ValueError):
        decide(session, d, False, "approver")


def test_scope_recommendation_is_valid_scope_definition(session, slice_result):
    from sdtf.scope.models import ScopeDefinition

    d = REGISTRY["scope_recommendation"]().run(session, slice_result["project_id"], {"company_code": "5000"}, "architect")
    defn = ScopeDefinition(**d.proposal["scope_definition"])
    assert defn.company_codes == ["5000"]


def test_carveout_ownership_proposes_for_every_pending_object(session, slice_result):
    from sdtf.demo import demo_scope_definition
    from sdtf.models import SapSystem
    from sdtf.scope.service import create_manifest, pending_dispositions

    src, tgt = session.get(SapSystem, slice_result["source_id"]), session.get(SapSystem, slice_result["target_id"])
    m = create_manifest(session, slice_result["project_id"], demo_scope_definition(src, tgt, name="ownership-agent"), "architect")
    pend = pending_dispositions(m)
    d = REGISTRY["carveout_ownership"]().run(session, slice_result["project_id"], {"manifest_id": m.id}, "architect")
    props = d.proposal["dispositions"]
    assert {p["node"] for p in props} == set(pend) and all(p["decision"] in ("TRANSFER", "RETAIN", "DUPLICATE", "REFERENCE") for p in props)
    assert any(p["decision"] == "RETAIN" and "Export-controlled" in p["rationale"] for p in props)


def test_completeness_and_residual_reports(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    comp = completeness_report(session, m)
    assert comp["complete"] is True and comp["pending_approvals"]["count"] == 0
    assert all(c["coverage_pct"] == 100.0 for c in comp["coverage"] if c["type"].startswith(("FI.", "SD.", "MM.", "PP.")))
    ic = comp["intercompany_balances"]
    assert ic and all(not b["counterpart_in_scope"] for b in ic)
    det = {d["key"]: d["count"] for d in comp["detections"]}
    assert det["cross_company_postings"] > 0 and det["shared_customers"] > 0 and det["sensitive_records"] > 0
    res = residual_exposure_report(session, m)
    assert res["residual_cleanup_candidates"]["count"] > 0 and res["tsa_services"]
    assert all(c["action"].endswith("AFTER_APPROVAL") for c in res["residual_cleanup_candidates"]["samples"])


def test_cutover_runbook_dependencies_and_critical_path(session, slice_result):
    m = session.get(ScopeManifest, slice_result["manifest_id"])
    rb = generate_runbook(session, m)
    ids = [t["id"] for t in rb["tasks"]]
    for t in rb["tasks"]:
        for d in t["depends_on"]:
            assert ids.index(d) < ids.index(t["id"])
            assert t["earliest_start_min"] >= next(x for x in rb["tasks"] if x["id"] == d)["earliest_finish_min"]
    assert rb["critical_path"][0] in ("T01", "T02") and rb["critical_path"][-1] == "T13"
    assert rb["point_of_no_return"] == "T10" and "T10" in rb["rollback"]["irreversible_tasks"]
    assert 0 < rb["forecast_downtime_minutes"] < rb["total_minutes"]
    assert set(rb["sign_offs"]["business"]) & set(rb["sign_offs"]["technical"]) == set()

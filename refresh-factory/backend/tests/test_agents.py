import pytest

from .conftest import ADMIN, ALICE, CAROL, make_project, ready_project
from rfactory.agents.framework import ACTIONS, Recommendation, ev
from rfactory.agents.service import AgentError, AgentService
from rfactory.security.auth import DEMO_USERS as U, Forbidden

COPILOT, SVC_CI, BASTIAN = U["refresh.copilot"], U["svc.ci"], U["bastian.lead"]


def run(svc, aid, actor=ALICE, **params):
    return svc.agents.run(actor, aid, params)


def snapshot(svc):
    return {"projects": {k: (p.status, p.manifest.content_hash() if p.manifest else None) for k, p in svc.projects.items()},
            "writable": {k: s.writable_target_allowed for k, s in svc.systems.items()},
            "profiles": len(svc.postcopy.profiles), "runs": len(svc.runs),
            "tables": {k: sum(a.table_counts().values()) for k, a in svc.adapters.items()}}


# ---- framework guarantees
def test_no_evidence_no_recommendation():
    with pytest.raises(ValueError, match="no evidence"):
        Recommendation("x", "t", "s", "low", "high", "b", ["why"], [])
    with pytest.raises(ValueError, match="no evidence"):
        Recommendation("x", "t", "s", "low", "high", "b", [], [ev("a", "b", "c")])


def test_action_flags_derive_from_kind_not_from_the_agent():
    r = Recommendation("x", "t", "s", "low", "high", "b", ["why"], [ev("a", "b", "c")], "purge_dataset")
    assert r.action["destructive"] and r.action["requires_approver"] and not r.action["executable"]
    with pytest.raises(ValueError):
        Recommendation("x", "t", "s", "low", "high", "b", ["why"], [ev("a", "b", "c")], "format_disk")


def test_every_destructive_or_production_action_is_not_executable():
    for k, a in ACTIONS.items():
        if a["destructive"] or a["production_access"]:
            assert not a["executable"], k


def test_catalogue_lists_twelve_agents(svc):
    c = svc.agents.catalogue()
    assert [a["number"] for a in c["agents"]] == list(range(1, 13))
    assert not c["narration_available"]


# ---- every agent produces grounded output on the demo landscape
def test_all_agents_run_and_every_recommendation_has_evidence(svc):
    p = ready_project(svc)
    cases = [("landscape-discovery", {}), ("refresh-strategy", {"project_id": p.id}), ("business-dependency", {"project_id": p.id}),
             ("scope-optimization", {"project_id": p.id}), ("masking-recommendation", {"project_id": p.id}), ("refresh-scheduling", {}),
             ("conflict-analysis", {"project_id": p.id}), ("postcopy-automation", {"system_id": svc.tgt_id}), ("reconciliation-analysis", {}),
             ("performance-optimization", {}), ("compliance-verification", {}), ("refresh-documentation", {"kind": "project", "id": p.id})]
    for aid, params in cases:
        r = svc.agents.run(ALICE, aid, params)
        assert r["summary"] and r["method"] and r["limitations"] is not None, aid
        for rec in r["recommendations"]:
            assert rec["evidence"] and rec["rationale"] and rec["confidence_basis"], aid
    assert svc.audit.verify()["valid"]


def test_agents_do_not_change_platform_state(svc):
    p = ready_project(svc)
    before = snapshot(svc)
    for aid, params in [("landscape-discovery", {}), ("scope-optimization", {"project_id": p.id}), ("masking-recommendation", {"project_id": p.id}),
                        ("conflict-analysis", {"project_id": p.id}), ("postcopy-automation", {"system_id": svc.tgt_id}), ("compliance-verification", {})]:
        svc.agents.run(ALICE, aid, params)
    assert snapshot(svc) == before


def test_missing_input_is_rejected(svc):
    with pytest.raises(ValueError):
        run(svc, "masking-recommendation")
    with pytest.raises(AgentError):
        run(svc, "no-such-agent")
    p = make_project(svc)
    with pytest.raises(ValueError, match="plan"):
        run(svc, "refresh-strategy", project_id=p.id)


# ---- specific agent behaviour
def test_landscape_flags_writable_production(svc):
    r = run(svc, "landscape-discovery")
    locks = [x for x in r["recommendations"] if x["action"]["kind"] == "lock_system"]
    assert {x["action"]["params"]["system_id"] for x in locks} == {svc.src_id, svc.s4_src}


def test_masking_agent_finds_uncovered_field(svc):
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    r = run(svc, "masking-recommendation", project_id=p.id)
    rec = next(x for x in r["recommendations"] if x["action"]["kind"] == "add_masking_rules")
    assert rec["priority"] == "critical" and rec["action"]["params"]["rules"]


def test_scope_agent_never_recommends_losing_too_many_selected_objects(svc):
    p = ready_project(svc)
    r = run(svc, "scope-optimization", project_id=p.id, min_root_fraction=0.99)
    assert not r["recommendations"]
    assert all(v["blocking"] == 0 for v in r["artifacts"]["variants"])


def test_compliance_detects_a_broken_audit_chain(svc):
    ready_project(svc)
    assert next(c for c in run(svc, "compliance-verification")["artifacts"]["controls"] if c["id"] == "C01")["status"] == "pass"
    svc.audit._entries[2]["actor"] = "mallory"
    r = run(svc, "compliance-verification")
    assert next(c for c in r["artifacts"]["controls"] if c["id"] == "C01")["status"] == "fail"


def test_compliance_detects_a_separation_of_duties_violation(svc):
    p = ready_project(svc)
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    assert next(c for c in run(svc, "compliance-verification")["artifacts"]["controls"] if c["id"] == "C02")["status"] == "pass"
    p.approval["by"] = p.created_by
    assert next(c for c in run(svc, "compliance-verification")["artifacts"]["controls"] if c["id"] == "C02")["status"] == "fail"


def test_reconciliation_agent_explains_a_held_run(svc):
    p = ready_project(svc)
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    pol = p.masking_policy
    pol.rules = [r for r in pol.rules if r.field != "STRAS"] if any(r.field == "STRAS" for r in pol.rules) else pol.rules[:-1]
    run_ = svc.execute(ALICE, p.id)
    r = run(svc, "reconciliation-analysis", run_id=run_.id)
    if run_.release == "HELD":
        assert r["recommendations"] and any("SEC" in x["title"] for x in r["recommendations"])
        assert any(x["action"]["kind"] == "rollback_run" and not x["action"]["executable"] for x in r["recommendations"])
    else:  # the run was clean: the agent must then say nothing is wrong
        assert not r["recommendations"]


def test_performance_agent_labels_itself_as_simulation(svc):
    p = ready_project(svc)
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    svc.execute(ALICE, p.id)
    r = run(svc, "performance-optimization")
    assert any("simulation" in x.lower() for x in r["limitations"]) and r["artifacts"]["step_seconds"]


def test_documentation_agent_uses_recorded_facts(svc):
    p = ready_project(svc)
    md = run(svc, "refresh-documentation", kind="project", id=p.id)["artifacts"]["markdown"]
    assert p.name in md and "Audit trail" in md and svc.system(p.target_id).label in md
    with pytest.raises(ValueError):
        run(svc, "refresh-documentation", kind="bogus", id="x")


def test_postcopy_agent_recommends_profile_then_run(svc):
    r = run(svc, "postcopy-automation", system_id=svc.tgt_id)
    assert r["recommendations"][0]["action"]["kind"] == "capture_profile"
    prof = svc.postcopy.capture_profile(ALICE, svc.tgt_id, "base")
    svc.postcopy.submit_profile(ALICE, prof.id)
    svc.postcopy.approve_profile(CAROL, prof.id)
    svc.postcopy.simulate_copy(ADMIN, svc.src_id, svc.tgt_id)
    r = run(svc, "postcopy-automation", system_id=svc.tgt_id)
    rec = next(x for x in r["recommendations"] if x["action"]["kind"] == "create_postcopy_run")
    assert rec["evidence"] and rec["action"]["params"]["profile_id"] == prof.id


# ---- governed action path
def test_agent_and_service_principals_cannot_decide_even_with_admin_roles(svc):
    rid = run(svc, "landscape-discovery")["recommendations"][0]["id"]
    for who in (COPILOT, SVC_CI):
        for fn in (svc.agents.accept, svc.agents.reject):
            with pytest.raises(Forbidden):
                fn(who, rid)
        with pytest.raises(Forbidden):
            svc.agents.apply(who, rid)
    assert svc.agents.recs[rid].status == "PROPOSED"


def test_apply_uses_the_humans_own_permissions(svc):
    rid = run(svc, "landscape-discovery")["recommendations"][0]["id"]  # lock_system needs system:write
    with pytest.raises(Forbidden):
        svc.agents.apply(U["bob.steward"], rid)
    out = svc.agents.apply(ALICE, rid)
    assert out["status"] == "APPLIED" and out["result"]["executed"]
    assert svc.system(out["action"]["params"]["system_id"]).writable_target_allowed is False
    assert any(e["action"] == "agent.recommendation.applied" and e["actor"] == ALICE.id for e in svc.audit.entries())
    with pytest.raises(AgentError):
        svc.agents.apply(ALICE, rid)  # no double apply


def test_applying_a_masking_recommendation_resets_approval(svc):
    p = make_project(svc)
    svc.build_plan(ALICE, p.id)
    rec = next(x for x in run(svc, "masking-recommendation", project_id=p.id)["recommendations"] if x["action"]["kind"] == "add_masking_rules")
    with pytest.raises(Forbidden):
        svc.agents.apply(CAROL, rec["id"])  # approvers do not hold masking:write
    svc.agents.apply(ALICE, rec["id"])
    assert svc.masking_state(p.id)["uncovered"] == 0
    assert p.approval is None


def test_scope_and_policy_recommendations_apply_and_reset_the_plan(svc):
    p = ready_project(svc)
    # force a scope recommendation by removing downstream types first
    svc.set_manifest(ALICE, p.id, p.manifest.scope, [], p.manifest.masking_policy_id, p.manifest.conflict_policy)
    svc.build_plan(ALICE, p.id)
    r = run(svc, "business-dependency", project_id=p.id)
    rec = next(x for x in r["recommendations"] if x["action"]["kind"] == "set_scope")
    v = p.manifest.version
    out = svc.agents.apply(ALICE, rec["id"])
    assert out["status"] == "APPLIED" and p.manifest.version == v + 1 and p.plan is None
    assert set(p.manifest.include_downstream) >= {"DELIVERY", "BILLING", "FI_DOCUMENT"}


def test_destructive_recommendations_are_handoff_only(svc):
    from rfactory.agents.framework import Recommendation as R
    rec = R("reconciliation-analysis", "Roll back", "s", "high", "high", "b", ["held"], [ev("run", "r1", "HELD")], "rollback_run", {"run_id": "run-1"})
    svc.agents.recs[rec.id] = rec
    with pytest.raises(Forbidden):
        svc.agents.apply(ALICE, rec.id)  # alice lacks plan:approve
    out = svc.agents.apply(ADMIN, rec.id)  # root.admin holds the approver role
    assert out["status"] == "HANDED_OFF" and out["result"]["executed"] is False
    assert out["result"]["handoff"]["path"] == "/api/runs/run-1/rollback"
    assert not svc.runs  # nothing was executed


def test_reject_records_the_decision(svc):
    rid = run(svc, "landscape-discovery")["recommendations"][0]["id"]
    out = svc.agents.reject(ALICE, rid, "intentional for this demo")
    assert out["status"] == "REJECTED" and out["note"]
    with pytest.raises(AgentError):
        svc.agents.apply(ALICE, rid)


# ---- narration and copilot
class Fake:
    model = "fake-model"

    def __init__(self):
        self.seen = []

    def __call__(self, system, user):
        self.seen.append(user)
        return "narrated text"


def test_narrator_only_attaches_text(svc):
    fake = Fake()
    svc.agents = AgentService(svc, narrator=fake)
    a = run_with_narration(svc)
    b = svc.agents.run(ALICE, "landscape-discovery", {}, narrate=False)
    assert a["narrative"]["text"] == "narrated text" and a["narrative"]["model"] == "fake-model" and "no authority" in a["narrative"]["disclaimer"]
    assert b["narrative"] is None
    strip = lambda r: [(x["title"], x["priority"], x["action"]["kind"]) for x in r["recommendations"]]
    assert strip(a) == strip(b)
    assert not any("PRD" in u and "KNA1" in u for u in fake.seen)  # only structured text goes out


def run_with_narration(svc):
    return svc.agents.run(ALICE, "landscape-discovery", {}, narrate=True)


def test_narrator_failure_or_absence_never_breaks_the_report(svc):
    r = svc.agents.run(ALICE, "landscape-discovery", {}, narrate=True)  # none configured
    assert r["narrative"] is None and any("not configured" in x for x in r["limitations"])

    def boom(s, u):
        raise RuntimeError("api down")
    svc.agents = AgentService(svc, narrator=boom)
    r = svc.agents.run(ALICE, "landscape-discovery", {}, narrate=True)
    assert r["narrative"] is None and r["recommendations"] and any("failed" in x for x in r["limitations"])


def test_copilot_routes_by_keyword_and_says_so(svc):
    out = svc.agents.copilot(ALICE, "Are we compliant?")
    assert out["routed_to"] == "compliance-verification" and "not an LLM" in out["method"]
    out = svc.agents.copilot(ALICE, "what masking do I need?")
    assert out["routed_to"] == "masking-recommendation" and out["needs"] == ["project_id"]
    assert svc.agents.copilot(ALICE, "tell me a joke")["routed_to"] is None


def test_kind_check_holds_even_for_a_service_principal_that_has_the_permission(svc):
    from rfactory.security.auth import Principal
    rid = run(svc, "landscape-discovery")["recommendations"][0]["id"]
    robot = Principal("svc.robot", "Robot", ("basis", "approver"), kind="service")
    assert robot.can("system:write")
    with pytest.raises(Forbidden):
        svc.agents.apply(robot, rid)


def test_performance_agent_reports_estimate_calibration(svc):
    r = run(svc, "performance-optimization")
    assert any("placeholder" in f["text"] for f in r["findings"]) and r["artifacts"]["estimate_calibration"]["models"] == 0
    svc.bench.run_benchmark(ALICE, svc.src_id, windows=(15, 30, 60, 90, 120, 180), repeats=2)
    r = run(svc, "performance-optimization")
    assert not any("placeholder" in f["text"] for f in r["findings"]) and r["artifacts"]["estimate_calibration"]["models"] >= 3

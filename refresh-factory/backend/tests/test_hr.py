"""HR master data (infotypes): special-category personal data with its own permission, mandatory per-run anonymization and checks."""
import pytest

from rfactory.masking.engine import CATALOG, HR_TABLES, MaskMode, MaskingEngine, Rule, template
from rfactory.reconcile.validator import reconcile
from rfactory.sap.synthetic import build_source_dataset
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.selective.manifest import Scope
from rfactory.service import Conflict

from .conftest import ADMIN, ALICE, CAROL, make_project
from .test_manufacturing import FAMILIES, pair

HANNA = U["hanna.hr"]
STRICT = "gdpr-strict"


def hr_project(svc, family="ECC", policy=STRICT, actor=HANNA, **kw):
    s, t = pair(svc, family)
    p = svc.create_project(actor, "hr", s, t)
    svc.set_manifest(actor, p.id, Scope(object_type="EMPLOYEE", **kw), [], policy, {"DUPLICATE_DIFFERENT": "SKIP", "DUPLICATE_IDENTICAL": "SKIP"})
    return p


def ready(svc, family="ECC", policy=STRICT, **kw):
    p = hr_project(svc, family, policy, **kw)
    svc.build_plan(HANNA, p.id)
    svc.analyze_conflicts(HANNA, p.id)
    return p


def run_it(svc, p):
    svc.submit(HANNA, p.id)
    svc.approve(CAROL, p.id)
    return svc.execute(HANNA, p.id)


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_hr_data_is_consistent(family):
    d = build_source_dataset(family)
    assert all(d[t] for t in ("PA0003", "PA0001", "PA0002", "PA0006", "PA0008", "PA0009"))
    for e in d["PA0003"]:
        n = e["PERNR"]
        assert [r for r in d["PA0001"] if r["PERNR"] == n] and [r for r in d["PA0002"] if r["PERNR"] == n] and [r for r in d["PA0009"] if r["PERNR"] == n]
        pay = next(r for r in d["PA0008"] if r["PERNR"] == n)
        assert abs(pay["ANSAL"] - round(pay["BET01"] * 12, 2)) < 0.01
    assert {c["BUKRS"] for c in d["PA0001"]} == {"1000", "2000"}


def test_every_hr_field_with_personal_data_is_in_the_masking_catalog():
    must = {("PA0002", "NACHN"), ("PA0002", "VORNA"), ("PA0002", "GBDAT"), ("PA0002", "PERID"), ("PA0006", "STRAS"), ("PA0006", "TELNR"), ("PA0008", "BET01"),
            ("PA0008", "ANSAL"), ("PA0009", "EMFTX"), ("PA0009", "BANKN"), ("PA0009", "BANKL")}
    assert must <= set(CATALOG) and {t for t, _ in must} <= HR_TABLES


# ---------------------------------------------------------------- the permission
def test_only_a_person_with_hr_copy_may_select_hr_data(svc):
    s, t = pair(svc, "ECC")
    for who in (ALICE, CAROL, ADMIN, U["tina.tester"], U["dave.privacy"], U["refresh.copilot"], U["svc.scheduler"]):
        p = svc.create_project(ALICE, "x", s, t)  # created by someone who may create projects
        with pytest.raises(Forbidden, match="hr:copy"):
            svc.set_manifest(who, p.id, Scope(object_type="EMPLOYEE"), [], STRICT, {})
    p = hr_project(svc)  # hanna may
    assert p.manifest.scope.object_type == "EMPLOYEE"
    assert "hr:copy" not in U["refresh.copilot"].permissions()


def test_planning_a_manifest_someone_else_wrote_still_needs_hr_copy(svc):
    p = hr_project(svc)
    with pytest.raises(Forbidden, match="hr:copy"):
        svc.build_plan(ALICE, p.id)
    assert p.plan is None
    svc.build_plan(HANNA, p.id)
    assert p.plan is not None


def test_hr_cannot_be_a_delta_scenario(svc):
    from .test_delta import spec
    with pytest.raises(Conflict, match="HR data cannot be refreshed incrementally"):
        svc.delta.create(HANNA, spec(svc, scopes=[{"scope": Scope(object_type="EMPLOYEE")}], include_downstream=[], schedule=None))


# ---------------------------------------------------------------- the masking rule
def test_stable_pseudonyms_are_refused_for_hr_and_anonymization_is_accepted(svc):
    p = ready(svc, policy="gdpr-standard")
    with pytest.raises(Conflict, match="per-run anonymization.*PSEUDONYMIZE"):
        svc.submit(HANNA, p.id)
    q = ready(svc, policy=STRICT)
    assert svc.submit(HANNA, q.id).status == "PENDING_APPROVAL"


def test_one_field_downgraded_to_pseudonymization_blocks_submission_and_resets_an_approval(svc):
    p = ready(svc)
    svc.submit(HANNA, p.id)
    svc.approve(CAROL, p.id)
    svc.add_masking_rules(HANNA, p.id, [{"table": "PA0008", "field": "BET01", "strategy": "AMOUNT", "category": "pay", "mode": "PSEUDONYMIZE"}])
    assert p.status != "APPROVED"  # editing the policy withdraws the approval
    with pytest.raises(Conflict):
        svc.execute(HANNA, p.id)
    svc.analyze_conflicts(HANNA, p.id)
    with pytest.raises(Conflict, match=r"PA0008.BET01 is PSEUDONYMIZE"):
        svc.submit(HANNA, p.id)


def test_a_policy_downgraded_behind_the_approval_is_caught_again_at_execution(svc):
    p = ready(svc)
    svc.submit(HANNA, p.id)
    svc.approve(CAROL, p.id)
    p.masking_policy.rules = [Rule(r.table, r.field, r.strategy, r.category, MaskMode.PSEUDONYMIZE) if (r.table, r.field) == ("PA0002", "NACHN") else r
                              for r in p.masking_policy.rules]  # tampering that bypasses add_masking_rules
    with pytest.raises(Conflict, match="per-run anonymization"):
        svc.execute(HANNA, p.id)


def test_amounts_are_perturbed_not_kept_and_not_proportional():
    eng = MaskingEngine(template(STRICT))
    rows = [{"PERNR": f"{i}", "ENDDA": "9999-12-31", "BET01": 4000.0 + i * 100, "ANSAL": 48000.0 + i * 1200, "WAERS": "EUR"} for i in range(60)]
    out = [eng.mask_row("PA0008", r) for r in rows]
    ratios = [o["BET01"] / r["BET01"] for o, r in zip(out, rows)]
    assert all(0.59 <= x <= 0.97 or 1.03 <= x <= 1.41 for x in ratios) and len({round(x, 2) for x in ratios}) > 20
    assert all(o["WAERS"] == "EUR" and o["PERNR"] == r["PERNR"] for o, r in zip(out, rows))
    assert eng.stats[("PA0008", "BET01")]["masked"] == 60
    keyed = MaskingEngine(template("gdpr-standard"), persistent_key=b"k" * 32)
    again = MaskingEngine(template("gdpr-standard"), persistent_key=b"k" * 32)
    assert keyed.mask_row("PA0008", rows[0]) == again.mask_row("PA0008", rows[0])  # a stable pseudonym (not allowed for HR) is stable
    assert eng.mask_row("PA0008", {"BET01": True})["BET01"] is True  # booleans are not amounts
    a, b = MaskingEngine(template(STRICT)), MaskingEngine(template(STRICT))
    assert a.mask_row("PA0008", rows[0])["BET01"] != b.mask_row("PA0008", rows[0])["BET01"]  # per-run: different every time


# ---------------------------------------------------------------- the refresh
@pytest.mark.parametrize("family", FAMILIES)
def test_employees_are_refreshed_masked_and_reconciled(svc, family):
    p = ready(svc, family, company_codes=["1000"])
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-HR-REFS"] == "pass" and checks["SEC-HR-ANON"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    mine = {e["PERNR"] for e in src.data["PA0001"] if e["BUKRS"] == "1000"}
    assert {r["PERNR"] for r in tgt.data["PA0003"]} == mine
    so = {r["PERNR"]: r for r in src.data["PA0002"]}
    sp = {r["PERNR"]: r for r in src.data["PA0008"]}
    sb = {r["PERNR"]: r for r in src.data["PA0009"]}
    for r in tgt.data["PA0002"]:
        o = so[r["PERNR"]]
        assert r["NACHN"] != o["NACHN"] and r["VORNA"] != o["VORNA"] and r["GBDAT"] != o["GBDAT"] and r["PERID"] != o["PERID"] and len(r["PERID"]) == len(o["PERID"])
        assert r["GESCH"] == o["GESCH"] and r["NATIO"] == o["NATIO"]
    for r in tgt.data["PA0008"]:
        o = sp[r["PERNR"]]
        assert r["BET01"] != o["BET01"] and r["ANSAL"] != o["ANSAL"] and r["WAERS"] == o["WAERS"]
    for r in tgt.data["PA0009"]:
        assert r["BANKN"] != sb[r["PERNR"]]["BANKN"] and r["EMFTX"] != sb[r["PERNR"]]["EMFTX"]
    assert {(r["PERNR"], r["BUKRS"], r["WERKS"]) for r in tgt.data["PA0001"]} == {(r["PERNR"], r["BUKRS"], r["WERKS"]) for r in src.data["PA0001"] if r["BUKRS"] == "1000"}


def test_a_scope_by_plant_selects_that_plants_employees(svc):
    p = ready(svc, plants=["1010"])
    keys = {i.key for i in p.plan.instances.values() if i.type == "EMPLOYEE"}
    src = svc.adapters[svc.src_id]
    assert keys and keys == {r["PERNR"] for r in src.data["PA0001"] if r["WERKS"] == "1010"}


def test_reconciliation_catches_a_missing_infotype_and_a_downgraded_policy(svc):
    p = ready(svc, company_codes=["1000"])
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check(cid, engine=None):
        rec = reconcile(run, p.plan, view, tgt, engine or svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == cid)
    assert check("BUS-HR-REFS")["status"] == "pass"
    gone = tgt.data["PA0008"][0]
    tgt.data["PA0008"] = tgt.data["PA0008"][1:]; tgt._idx.clear()
    assert check("BUS-HR-REFS")["status"] == "fail"
    tgt.data["PA0008"].insert(0, gone); tgt._idx.clear()
    assert check("BUS-HR-REFS")["status"] == "pass"
    weak = MaskingEngine(template("gdpr-standard"))
    assert check("SEC-HR-ANON", weak)["status"] == "fail"


def test_unmasked_numbers_are_caught_as_residual_pii(svc):
    p = ready(svc, company_codes=["1000"])
    run = run_it(svc, p)
    tgt, src = svc.adapters[svc.tgt_id], svc.adapters[svc.src_id]
    row = tgt.data["PA0008"][0]
    row["BET01"] = next(r for r in src.data["PA0008"] if r["PERNR"] == row["PERNR"])["BET01"]  # an original salary slipped through
    tgt._idx.clear()
    rec = reconcile(run, p.plan, svc.source_view(p.source_id), tgt, svc.engines[p.id], svc.registries["ECC"], svc.required_sensitive.get(p.id, []))
    assert next(c for c in rec["checks"] if c["id"] == "SEC-RESIDUAL")["status"] == "fail"


def test_a_missing_rule_and_an_agent_with_the_role_are_both_refused(svc):
    from rfactory.security import authz
    from rfactory.security.auth import Principal
    p = ready(svc)
    pol = template(STRICT)
    pol.rules = [r for r in pol.rules if (r.table, r.field) != ("PA0002", "NACHN")]
    assert authz.hr_masking_violations(p.plan, pol) == ["PA0002.NACHN has no masking rule"]
    assert authz.hr_masking_violations(p.plan, None)  # no policy at all
    bot = Principal("bot", "bot", ("hr_steward",), kind="agent")
    assert not bot.can("hr:copy")
    with pytest.raises(Forbidden):
        authz.require_hr(bot, "EMPLOYEE")

import copy
from datetime import datetime, timedelta, timezone

import pytest

from rfactory.sap.ddic import TABLES
from rfactory.security.auth import DEMO_USERS as U, Forbidden
from rfactory.service import Conflict
from rfactory.tdm.templates import PLANNED, TEMPLATES, find_candidates
from .conftest import ADMIN, ALICE, BOB, CAROL, approved_project, norm

TINA, TOM, CI, SCHED, COPILOT = U["tina.tester"], U["tom.tester"], U["svc.ci"], U["svc.scheduler"], U["refresh.copilot"]
T0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def ids(svc, family):
    return (svc.s4_src, svc.s4_tgt) if family == "S4" else (svc.src_id, svc.tgt_id)


def policy(svc, family="ECC", **over):
    s, t = ids(svc, family)
    p = svc.tdm.create_policy(ALICE, {"name": f"QA self-service {family}", "source_id": s, "target_id": t, **over})
    svc.tdm.submit_policy(ALICE, p.id)
    svc.tdm.approve_policy(CAROL, p.id)
    return p


def ask(svc, user, template, family="ECC", **kw):
    spec = {"target_id": ids(svc, family)[1], "template_id": template, "reserve": kw.pop("reserve", False), **kw}
    return svc.tdm.request(user, spec, now=kw.get("now", T0)) if "now" not in kw else svc.tdm.request(user, spec, now=kw.pop("now"))


def bootstrap_masters(svc, family="ECC"):
    r = ask(svc, ALICE, "md_materials", family, mode="subset", count=8)
    assert r["status"] == "FULFILLED", r
    return r


# ------------------------------------------------------------------ templates and finders
def test_templates_available_and_planned_are_honest(svc):
    t = svc.tdm.templates()
    assert {"o2c_complete", "p2p_purchase_order", "r2r_billing_posting"} <= {x["id"] for x in t["available"]}
    planned = {p["id"]: p for p in t["planned"]}
    assert {"make_to_order", "asset_accounting", "inventory_wm"} <= set(planned)
    assert all(p["reason"] for p in planned.values())
    assert "Goods receipt" in next(x for x in t["available"] if x["id"] == "p2p_purchase_order")["coverage"]


def test_candidates_really_have_the_advertised_shape(svc):
    src = svc.adapters[svc.src_id]
    p = {"company_code": "1000", "days": 90}
    complete = find_candidates(TEMPLATES["o2c_complete"], src, p)
    assert complete
    for c in complete:
        dl = {l["VBELN"] for l in src.lookup("LIPS", "VGBEL", c["key"])}
        bl = {b["VBELN"] for d in dl for b in src.lookup("VBRP", "VGBEL", d)}
        assert dl and bl and any(h["AWKEY"] in bl for b in bl for h in src.lookup("BKPF", "AWKEY", b))
    for c in find_candidates(TEMPLATES["o2c_order_only"], src, p):
        assert not src.lookup("LIPS", "VGBEL", c["key"])
    for c in find_candidates(TEMPLATES["o2c_delivered_unbilled"], src, p):
        dl = {l["VBELN"] for l in src.lookup("LIPS", "VGBEL", c["key"])}
        assert dl and not any(src.lookup("VBRP", "VGBEL", d) for d in dl)
    plant_cc = {x["WERKS"]: x["BUKRS"] for x in src.select("T001W")}
    for c in find_candidates(TEMPLATES["o2c_cross_company_plant"], src, p):
        assert any(plant_cc[i["WERKS"]] != "1000" for i in src.lookup("VBAP", "VBELN", c["key"]))
    keys = lambda t: {c["key"] for c in find_candidates(TEMPLATES[t], src, p)}
    assert not keys("o2c_complete") & keys("o2c_order_only") and not keys("o2c_complete") & keys("o2c_delivered_unbilled")


# ------------------------------------------------------------------ policy governance
def test_no_policy_no_data_and_planned_templates_rejected(svc):
    r = ask(svc, TINA, "o2c_complete", count=1)
    assert r["status"] == "REJECTED" and "no approved TDM policy" in r["reasons"][0]
    policy(svc)
    r = ask(svc, TINA, "make_to_order")
    assert r["status"] == "REJECTED" and "sales-order-based planning" in r["reasons"][0]
    with pytest.raises(Forbidden):
        svc.tdm.request(U["erin.auditor"], {"target_id": svc.tgt_id, "template_id": "o2c_complete"})


def test_policy_sod_single_active_masking_and_target_guards(svc):
    s, t = ids(svc, "ECC")
    p = svc.tdm.create_policy(ALICE, {"name": "p", "source_id": s, "target_id": t})
    assert any(r["field"] == "ZZ_CONTACT_EMAIL" for r in p.masking_rules)  # custom PII auto-added, visible to the approver
    svc.tdm.submit_policy(ALICE, p.id)
    for who in (ALICE, COPILOT, TINA):
        with pytest.raises(Forbidden):
            svc.tdm.approve_policy(who, p.id)
    svc.tdm.approve_policy(CAROL, p.id)
    p2 = svc.tdm.create_policy(ALICE, {"name": "p2", "source_id": s, "target_id": t})
    svc.tdm.submit_policy(ALICE, p2.id); svc.tdm.approve_policy(CAROL, p2.id)
    assert p.status == "SUSPENDED" and p2.status == "ACTIVE"
    with pytest.raises(Forbidden):
        svc.tdm.create_policy(ALICE, {"name": "x", "source_id": t, "target_id": s})        # production target
    with pytest.raises(Conflict):
        svc.tdm.create_policy(ALICE, {"name": "x", "source_id": s, "target_id": svc.s4_tgt})  # ECC -> S/4
    with pytest.raises(Conflict):
        svc.tdm.create_policy(ALICE, {"name": "x", "source_id": s, "target_id": t, "allowed_templates": ["nope"]})


# ------------------------------------------------------------------ subset provisioning
@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_subset_o2c_provisions_complete_masked_chain_and_reserves(svc, family):
    policy(svc, family)
    tgt = svc.adapters[ids(svc, family)[1]]
    r = ask(svc, TINA, "o2c_complete", family, mode="subset", count=2, reserve=True, params={"days": 90},
            purpose="regression CHG-1", test_cases=[{"system": "jira", "id": "QA-101", "title": "Create invoice"}])
    assert r["status"] == "FULFILLED" and r["approval"]["by"].startswith("policy:"), r
    assert len(r["datasets"]) == 2
    for ref in r["datasets"]:
        d = svc.tdm.get(ref["id"])
        assert d.state == "RESERVED" and d.reserved_by == "tina.tester" and d.provenance == "subset"
        h = d.handles
        assert h["sales_order"] and h["deliveries"] and h["billing_documents"] and h["accounting_documents"] and h["customer"]
        assert tgt.get("VBAK", (h["sales_order"],)) and tgt.get("VBRK", (h["billing_documents"][0],))
        assert svc.tdm.verify(d.id)["intact"] and d.test_cases[0]["id"] == "QA-101"
    run = svc.runs[r["run_id"]]
    assert run.reconciliation["release"] == "RELEASED"
    src = svc.adapters[ids(svc, family)[0]]
    blob = str(tgt.data)
    for k in src.data["KNA1"]:
        assert k["NAME1"] not in blob and k["STCD1"] not in blob and k["ZZ_CONTACT_EMAIL"] not in blob


def test_subset_never_picks_roots_the_target_cannot_host(svc):
    policy(svc)
    r = ask(svc, TINA, "o2c_cross_company_plant", mode="subset", count=1, params={"days": 90})
    assert r["status"] == "FAILED" and any("lacks customizing" in n for n in r["notes"])  # plant 2000 is not customized in QA
    assert not svc.tdm.datasets


def test_auto_mode_prefers_catalog_and_reservations_are_exclusive(svc):
    policy(svc)
    first = ask(svc, TINA, "o2c_complete", mode="subset", count=1, params={"days": 90}, reserve=True)
    d = svc.tdm.get(first["datasets"][0]["id"])
    with pytest.raises(Conflict, match="reserved by tina"):
        svc.tdm.reserve(TOM, d.id)
    with pytest.raises(Forbidden):
        svc.tdm.release(TOM, d.id)
    svc.tdm.release(TINA, d.id)
    second = ask(svc, TOM, "o2c_complete", mode="auto", count=1, params={"days": 90}, reserve=True)
    assert second["datasets"][0]["id"] == d.id and second["datasets"][0]["source"] == "catalog"
    assert d.reserved_by == "tom.tester" and "run_id" not in second  # nothing was provisioned


def test_gate_failure_rolls_the_target_back_and_registers_nothing(svc):
    policy(svc)
    tgt = svc.adapters[svc.tgt_id]
    before = copy.deepcopy(tgt.data)
    tgt.outbound_interfaces()[0]["active"] = True  # security gate must fail
    r = ask(svc, TINA, "o2c_complete", mode="subset", count=1, params={"days": 90})
    assert r["status"] == "FAILED" and any("release gate failed" in n for n in r["notes"])
    assert norm(tgt.data) == norm(before) and not svc.tdm.datasets


# ------------------------------------------------------------------ quotas and approvals
def test_ttl_reservation_quota_and_approval_flow(svc):
    policy(svc, max_objects=2, max_active_reservations=2, max_ttl_days=5)
    assert ask(svc, TINA, "o2c_order_only", mode="subset", count=1, ttl_days=30)["status"] == "REJECTED"
    over = ask(svc, TINA, "o2c_order_only", mode="subset", count=3, params={"days": 90}, reserve=False)
    assert over["status"] == "PENDING_APPROVAL" and not svc.tdm.datasets
    for who in (TINA, COPILOT, ALICE):
        with pytest.raises(Forbidden):
            svc.tdm.approve_request(who, over["id"])
    done = svc.tdm.approve_request(CAROL, over["id"], now=T0)
    assert done["approval"]["by"] == "carol.approver" and done["status"] in ("FULFILLED", "PARTIAL")
    got = ask(svc, TINA, "o2c_order_only", mode="catalog", count=2, reserve=True)
    assert got["status"] == "FULFILLED"
    rej = ask(svc, TINA, "o2c_order_only", mode="catalog", count=1, reserve=True)
    assert rej["status"] == "REJECTED" and "quota" in rej["reasons"][0]
    r2 = ask(svc, TOM, "o2c_order_only", mode="subset", count=3, params={"days": 90})
    assert svc.tdm.reject_request(CAROL, r2["id"], "too large")["status"] == "REJECTED"


# ------------------------------------------------------------------ synthetic generation
@pytest.mark.parametrize("family", ["ECC", "S4"])
def test_synthetic_o2c_is_consistent_masked_and_number_range_safe(svc, family):
    policy(svc, family)
    tgt = svc.adapters[ids(svc, family)[1]]
    pre = ask(svc, TINA, "o2c_complete", family, mode="synthetic", count=1)
    assert pre["status"] == "FAILED" and any("no materials" in n for n in pre["notes"])  # clean target needs master data first
    bootstrap_masters(svc, family)
    level = tgt.number_level("SD_ORDER")
    r = ask(svc, TINA, "o2c_complete", family, mode="synthetic", count=3, reserve=True)
    assert r["status"] == "FULFILLED" and all(x["source"] == "synthetic" for x in r["datasets"]), r
    run = svc.runs[r["run_id"]]
    assert run.reconciliation["release"] == "RELEASED"
    assert tgt.number_level("SD_ORDER") >= max(int(svc.tdm.get(x["id"]).handles["sales_order"]) for x in r["datasets"]) > level
    for x in r["datasets"]:
        d = svc.tdm.get(x["id"]); h = d.handles
        o = tgt.get("VBAK", (h["sales_order"],))
        assert o["ERNAM"] == "TDM_SYNTH" and int(h["customer"]) >= 9_000_000_001 and d.provenance == "synthetic"
        assert abs(o["NETWR"] - sum(i["NETWR"] for i in tgt.lookup("VBAP", "VBELN", o["VBELN"]))) < 0.01
        assert tgt.get("KNB1", (h["customer"], "1000")) and tgt.get("LIKP", (h["deliveries"][0],))
        if family == "S4":
            assert tgt.get("BUT000", (h["customer"],)) and tgt.lookup("ACDOCA", "AWREF", h["billing_documents"][0])
        assert svc.tdm.verify(d.id)["intact"]
    assert len({svc.tdm.get(x["id"]).handles["customer"] for x in r["datasets"]}) == 3  # no collisions between generated objects


def test_synthetic_stages_and_other_scenarios(svc):
    policy(svc); bootstrap_masters(svc)
    tgt = svc.adapters[svc.tgt_id]
    h = {t: svc.tdm.get(ask(svc, TINA, t, mode="synthetic", count=1)["datasets"][0]["id"]).handles
         for t in ("o2c_order_only", "o2c_delivered_unbilled", "p2p_purchase_order", "md_customer_with_bank", "md_vendor_with_bank")}
    assert "deliveries" not in h["o2c_order_only"]
    assert h["o2c_delivered_unbilled"]["deliveries"] and "billing_documents" not in h["o2c_delivered_unbilled"]
    po = h["p2p_purchase_order"]
    assert tgt.get("EKKO", (po["purchase_order"],)) and int(po["vendor"]) >= 8_000_000_001
    assert tgt.lookup("KNBK", "KUNNR", h["md_customer_with_bank"]["customer"]) and tgt.lookup("LFBK", "LIFNR", h["md_vendor_with_bank"]["vendor"])


# ------------------------------------------------------------------ discovery of what already exists
def test_scan_catalogs_existing_target_data_and_skips_tester_owned(svc):
    p = approved_project(svc)
    svc.execute(ALICE, p.id)  # a normal selective refresh into the QA target
    policy(svc)
    assert svc.tdm.catalog() == []
    out = svc.tdm.scan(ALICE, svc.tgt_id)
    assert out["added"] > 0 and out["skipped_tester_owned"] >= 1
    found = svc.tdm.catalog(provenance="discovered")
    assert found and all(d["provenance"] == "discovered" and d["state"] == "AVAILABLE" for d in found)
    assert any(d["template_id"] == "o2c_complete" for d in found) and "o2c_complete" in {d["template_id"] for d in found}
    owners = svc.adapters[svc.tgt_id].owners
    assert not any(d["root"].split(":", 1)[1] in {k.split("/", 1)[1] for k in owners} and d["root"].startswith("SALES_ORDER") for d in found)
    assert svc.tdm.scan(ALICE, svc.tgt_id)["added"] == 0  # idempotent
    d = svc.tdm.get(found[0]["id"])
    with pytest.raises(Conflict, match="provisioned"):
        svc.tdm.promote_golden(ALICE, d.id)
    svc.tdm.retire(ALICE, d.id)
    with pytest.raises(Conflict, match="not created by the platform"):
        svc.tdm.purge(CAROL, d.id)
    with pytest.raises(Forbidden):
        svc.tdm.scan(TINA, svc.tgt_id)
    r = ask(svc, TINA, "o2c_complete", mode="catalog", count=1, reserve=True)  # catalog fulfilment from discovered data
    assert r["status"] == "FULFILLED" and r["datasets"][0]["source"] == "catalog"


# ------------------------------------------------------------------ lifecycle
def test_sweep_expires_reservations_and_datasets(svc):
    policy(svc, retention_days=10)
    r = ask(svc, TINA, "o2c_order_only", mode="subset", count=2, params={"days": 90}, reserve=True, ttl_days=2)
    a, b = (svc.tdm.get(x["id"]) for x in r["datasets"])
    with pytest.raises(Forbidden):
        svc.tdm.sweep(U["erin.auditor"])
    out = svc.tdm.sweep(SCHED, T0 + timedelta(days=3))
    assert set(out["reservations_released"]) == {a.id, b.id} and a.state == "AVAILABLE" and a.reserved_by is None
    out = svc.tdm.sweep(SCHED, T0 + timedelta(days=11))
    assert set(out["datasets_expired"]) == {a.id, b.id} and a.state == "EXPIRED"
    assert svc.tdm.catalog() == [] and len(svc.tdm.catalog(include_inactive=True)) == 2
    with pytest.raises(Conflict):
        svc.tdm.reserve(TINA, a.id)


def test_purge_rules_and_shared_objects(svc):
    policy(svc)
    tgt = svc.adapters[svc.tgt_id]
    r = ask(svc, TINA, "o2c_order_only", mode="subset", count=2, params={"days": 90})
    d1, d2 = (svc.tdm.get(x["id"]) for x in r["datasets"])
    with pytest.raises(Forbidden):
        svc.tdm.purge(TINA, d1.id)
    with pytest.raises(Forbidden):
        svc.tdm.purge(COPILOT, d1.id)
    with pytest.raises(Conflict, match="EXPIRED or RETIRED"):
        svc.tdm.purge(CAROL, d1.id)
    svc.tdm.reserve(TOM, d2.id); 
    with pytest.raises(Conflict):
        svc.tdm.retire(ALICE, d2.id)
    svc.tdm.release(TOM, d2.id)
    d2.objects.append(d1.owned[0])  # d2 also depends on d1's first owned object -> shared
    shared = d1.owned[0]
    svc.tdm.retire(ALICE, d1.id)
    t, k = d1.root.split(":", 1)
    tgt.get("VBAK", (k,))["ERNAM"] = "SOMEONE"  # modified after provisioning
    with pytest.raises(Conflict, match="changed since provisioning"):
        svc.tdm.purge(CAROL, d1.id)
    out = svc.tdm.purge(CAROL, d1.id, force=True)
    assert shared in out["objects_kept_because_shared"] and d1.state == "PURGED"
    assert tgt.get("VBAK", (k,)) is None and tgt.lookup("VBAP", "VBELN", k) == []
    ty, key = shared.split(":", 1)
    assert tgt.get(svc.registries["ECC"].types[ty].header, tuple(key.split("/"))) is not None  # shared object survived
    assert svc.audit.verify()["valid"] and any(e["action"] == "tdm.dataset.purged" for e in svc.audit.entries())


def test_golden_dataset_verify_consume_restore(svc):
    policy(svc)
    tgt = svc.adapters[svc.tgt_id]
    r = ask(svc, TINA, "o2c_complete", mode="subset", count=1, params={"days": 90}, reserve=True)
    d = svc.tdm.get(r["datasets"][0]["id"])
    with pytest.raises(Forbidden):
        svc.tdm.promote_golden(TINA, d.id)
    svc.tdm.promote_golden(ALICE, d.id)
    assert d.golden and d.snapshot
    order = tgt.get("VBAK", (d.handles["sales_order"],))
    pristine = copy.deepcopy(tgt.data)
    # the test changes the order and posts an extra item
    order["NETWR"] += 999; order["ERNAM"] = "TEST"
    tgt.data["VBAP"].append({"VBELN": order["VBELN"], "POSNR": "000999", "MATNR": tgt.data["MARA"][0]["MATNR"], "WERKS": "1000", "KWMENG": 1, "NETWR": 1.0}); tgt._idx.clear()
    v = svc.tdm.verify(d.id)
    assert not v["intact"] and v["drift_count"] >= 1
    with pytest.raises(Conflict, match="drifted"):
        svc.tdm.promote_golden(ALICE, d.id)
    svc.tdm.record_usage(TINA, d.id, {"system": "jira", "id": "QA-7", "title": "Post goods issue"}, "passed", consumed=True)
    assert d.state == "CONSUMED" and d.reserved_by is None
    svc.tdm.restore(ALICE, d.id)
    assert d.state == "AVAILABLE" and svc.tdm.verify(d.id)["intact"]
    assert norm(tgt.data) == norm(pristine), "restore must reproduce the snapshot exactly, including removal of added items"
    assert any(h["event"] == "restored" for h in d.history)


def test_restore_requires_golden(svc):
    policy(svc)
    d = svc.tdm.get(ask(svc, TINA, "o2c_order_only", mode="subset", count=1, params={"days": 90})["datasets"][0]["id"])
    with pytest.raises(Conflict, match="not golden"):
        svc.tdm.restore(ALICE, d.id)


# ------------------------------------------------------------------ test-case association
def test_test_case_association_usage_and_search(svc):
    policy(svc)
    r = ask(svc, TINA, "o2c_order_only", mode="subset", count=2, params={"days": 90}, reserve=True)
    a, b = (svc.tdm.get(x["id"]) for x in r["datasets"])
    tc = {"system": "cloud_alm", "id": "TC-42", "title": "Create delivery"}
    svc.tdm.link_test_case(TINA, a.id, tc)
    assert [d["id"] for d in svc.tdm.catalog(test_case="TC-42")] == [a.id]
    with pytest.raises(Conflict, match="holding the reservation"):
        svc.tdm.record_usage(TOM, a.id, tc, "passed")
    with pytest.raises(Conflict, match="outcome"):
        svc.tdm.record_usage(TINA, a.id, tc, "maybe")
    svc.tdm.record_usage(TINA, a.id, tc, "failed", note="delivery block")
    assert a.usage[-1]["outcome"] == "failed" and a.state == "RESERVED"
    svc.tdm.link_test_case(TINA, a.id, tc, unlink=True)
    assert svc.tdm.catalog(test_case="TC-42") == []
    assert [d["id"] for d in svc.tdm.catalog(reserved_by="tina.tester")] and svc.tdm.catalog(q=a.handles["sales_order"])[0]["id"] == a.id


def test_audit_trail_covers_the_tdm_lifecycle(svc):
    policy(svc)
    r = ask(svc, TINA, "o2c_order_only", mode="subset", count=1, params={"days": 90}, reserve=True)
    svc.tdm.release(TINA, r["datasets"][0]["id"])
    acts = {e["action"] for e in svc.audit.entries()}
    assert {"tdm.policy.approved", "tdm.request.submitted", "tdm.dataset.provisioned", "tdm.dataset.reserved", "tdm.dataset.released",
            "tdm.request.completed", "run.completed"} <= acts
    assert svc.audit.verify()["valid"]

"""Manufacturing (PP) and inventory (MM-IM) objects: BOMs, production orders, material documents (ECC MKPF/MSEG, S/4HANA MATDOC)."""
import json
from datetime import datetime, timezone

import pytest

from rfactory.dependency.planner import Planner
from rfactory.sap.ddic import TABLES
from rfactory.sap.synthetic import REF_DATE, build_source_dataset
from rfactory.security.auth import DEMO_USERS as U
from rfactory.selective.manifest import Scope

from .conftest import ALICE, CAROL, make_project, ready_project
from .test_delta import approved as approved_delta
from .test_tdm import TINA, ask, bootstrap_masters, ids, policy

FAMILIES = ["ECC", "S4"]
PP_CHECKS = {"BUS-PP-STRUCT", "BUS-PP-BOM", "BUS-PP-COMPONENTS", "BUS-PP-MOVEMENTS"}


def pair(svc, family):
    return (svc.src_id, svc.tgt_id) if family == "ECC" else (svc.s4_src, svc.s4_tgt)


def mfg_project(svc, family, root="PRODUCTION_ORDER", downstream=("MATERIAL_DOCUMENT",), **kw):
    s, t = pair(svc, family)
    kw.setdefault("days", 0)
    if root == "PRODUCTION_ORDER":
        kw.setdefault("company", "1000")
    else:
        kw.setdefault("company", None)
        kw.setdefault("plants", ["1000"])
    return ready_project(svc, src=s, tgt=t, object_type=root, downstream=downstream, **kw)


def run_it(svc, p):
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    return svc.execute(ALICE, p.id)


# ---------------------------------------------------------------- the model
@pytest.mark.parametrize("family", FAMILIES)
def test_registry_is_valid_and_family_specific(svc, family):
    s, _ = pair(svc, family)
    reg = svc.registries[svc.system(s).family]
    assert reg.validate()["ok"]
    assert {"BOM", "PRODUCTION_ORDER", "MATERIAL_DOCUMENT"} <= set(reg.types)
    md = reg.types["MATERIAL_DOCUMENT"]
    if family == "ECC":
        assert [l.table for l in md.tables] == ["MKPF", "MSEG"] and md.header_keys == ("MBLNR", "MJAHR")
    else:  # S/4HANA keeps material documents in ONE table: a line is the unit
        assert [l.table for l in md.tables] == ["MATDOC"] and md.header_keys == ("MBLNR", "MJAHR", "ZEILE")
        assert all(r.via_table != "MSEG" for r in reg.relationships)
    names = {r.name for r in reg.relationships}
    assert {"production order→bom", "goods movement→production order", "bom→component"} <= names
    assert any(e["name"] == "goods movement→production order" and e["downstream"] for e in reg.graph()["edges"])


def test_synthetic_manufacturing_data_is_consistent_in_both_families():
    ecc, s4 = build_source_dataset(family="ECC"), build_source_dataset(family="S4")
    assert ecc["MATDOC"] == [] and s4["MKPF"] == [] and s4["MSEG"] == []
    assert len(s4["MATDOC"]) == len(ecc["MSEG"]) and {k for r in s4["MATDOC"] for k in r} == set(TABLES["MATDOC"].fields)
    assert s4["AUFK"] == ecc["AUFK"] and s4["STKO"] == ecc["STKO"]  # the same business content, different storage
    for d, lines in ((ecc, ecc["MSEG"]), (s4, s4["MATDOC"])):
        boms = {b["STLNR"]: b for b in d["STKO"]}
        for o in d["AFKO"]:
            bom = boms[o["STLNR"]]
            item = next(i for i in d["AFPO"] if i["AUFNR"] == o["AUFNR"])
            assert item["MATNR"] == bom["MATNR"] and item["PSMNG"] == o["GAMNG"]
            for comp in (c for c in d["STPO"] if c["STLNR"] == o["STLNR"]):
                r = next(x for x in d["RESB"] if x["AUFNR"] == o["AUFNR"] and x["MATNR"] == comp["IDNRK"])
                assert r["BDMNG"] == comp["MENGE"] * o["GAMNG"] / bom["BMENG"]
            mv = [l for l in lines if l["AUFNR"] == o["AUFNR"]]
            if item["WEMNG"]:
                assert sum(l["MENGE"] for l in mv if l["BWART"] == "101") == item["WEMNG"]
                for r in (x for x in d["RESB"] if x["AUFNR"] == o["AUFNR"]):
                    assert sum(l["MENGE"] for l in mv if l["BWART"] == "261" and l["MATNR"] == r["MATNR"]) == r["ENMNG"]
            else:
                assert not mv
    assert max(int(r["AUFNR"]) for r in ecc["AUFK"]) == next(n["NRLEVEL"] for n in ecc["NRIV"] if n["OBJECT"] == "PP_ORDER")
    assert any(not l["AUFNR"] for l in ecc["MSEG"])  # stock postings without an order exist too


@pytest.mark.parametrize("family", FAMILIES)
def test_source_relationships_are_clean(svc, family):
    s, _ = pair(svc, family)
    v = Planner(svc.source_view(s), svc.registries[svc.system(s).family]).validate_relationships()
    assert v["orphans"] == 0 and v["dangling"] == 0, v


@pytest.mark.parametrize("family", FAMILIES)
def test_discovery_counts_manufacturing_objects(svc, family):
    s, _ = pair(svc, family)
    bo = svc.discover(s)["business_objects"]
    assert bo["boms"] == 8 and bo["production_orders"] == 18 and bo["material_documents"] > 0


# ---------------------------------------------------------------- plans
@pytest.mark.parametrize("family", FAMILIES)
def test_order_plan_pulls_bom_materials_and_optionally_the_goods_movements(svc, family):
    p = mfg_project(svc, family, downstream=())
    plan = p.plan
    by = plan.summary()["by_type"]
    assert by["PRODUCTION_ORDER"] == 14 and by["BOM"] >= 1 and by["MATERIAL"] >= 2 and "MATERIAL_DOCUMENT" not in by
    order = next(i for i in plan.instances.values() if i.type == "PRODUCTION_ORDER")
    assert any(r.startswith("BOM:") for r in order.requires) and {"AUFK", "AFKO", "AFPO", "RESB"} <= set(order.rows)
    assert plan.order.index(next(r for r in order.requires if r.startswith("BOM:"))) < plan.order.index(order.id)  # BOM loads first
    assert plan.config_refs["PLANT"] and not plan.blocking
    q = mfg_project(svc, family)
    assert q.plan.summary()["by_type"]["MATERIAL_DOCUMENT"] > 0 and q.plan.summary()["by_origin"]["DOWNSTREAM"] > 0


@pytest.mark.parametrize("family", FAMILIES)
def test_scoping_by_material_document_pulls_the_order_it_belongs_to(svc, family):
    p = mfg_project(svc, family, root="MATERIAL_DOCUMENT", downstream=(), plants=["1000"], company=None)
    by = p.plan.summary()["by_type"]
    assert by["MATERIAL_DOCUMENT"] > 0 and by["PRODUCTION_ORDER"] > 0 and by["BOM"] > 0
    assert all(any(x.startswith("PRODUCTION_ORDER:") for x in i.requires) for i in p.plan.instances.values()
               if i.type == "MATERIAL_DOCUMENT" and any(r.get("AUFNR") for rows in i.rows.values() for r in rows))


def test_bom_scope_and_filters(svc):
    p = mfg_project(svc, "ECC", root="BOM", downstream=())
    assert p.plan.summary()["by_type"]["BOM"] == 6
    bad = make_project(svc, object_type="BOM", company="1000", days=0)
    svc.build_plan(ALICE, bad.id)
    assert any(i.code == "UNSUPPORTED_FILTER" for i in bad.plan.blocking)  # a BOM has no company code: say so, do not guess


def test_dangling_bom_reference_blocks_the_plan(svc):
    src = svc.adapters[svc.src_id]
    src.sim_update("AFKO", (next(a["AUFNR"] for a in src.data["AFKO"]),), STLNR="99999999")
    p = mfg_project(svc, "ECC", downstream=())
    assert any(i.code == "DANGLING_REFERENCE" and "BOM" in i.message for i in p.plan.blocking)


# ---------------------------------------------------------------- selective refresh end to end
@pytest.mark.parametrize("family", FAMILIES)
def test_production_orders_refresh_and_reconcile_in_both_families(svc, family):
    p = mfg_project(svc, family)
    findings = {(f["type"], f["severity"]) for f in svc.conflicts(p.id)["findings"]}
    assert ("DUPLICATE_DIFFERENT", "warning") in findings and ("NUMBER_RANGE", "warning") in findings  # the tester's order and the lagging ranges
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED"
    ids_ = {c["id"] for c in run.reconciliation["checks"]}
    assert PP_CHECKS <= ids_ and not run.reconciliation["failed"]
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    clash = next(a for a in tgt.data["AUFK"] if a["ERNAM"] == "QA_ALICE")
    assert clash == next(a for a in tgt.data["AUFK"] if a["AUFNR"] == clash["AUFNR"]) and tgt.get("AUFK", (clash["AUFNR"],))["ERNAM"] == "QA_ALICE"  # tester's order untouched
    loaded_orders = {i.key for i in p.plan.instances.values() if i.type == "PRODUCTION_ORDER" and i.id in run.loaded}
    assert clash["AUFNR"] not in loaded_orders and len(loaded_orders) >= 10
    for n in loaded_orders:
        assert tgt.get("AFKO", (n,)) == src.get("AFKO", (n,)) and len(tgt.lookup("RESB", "AUFNR", n)) == len(src.lookup("RESB", "AUFNR", n))
    lines_t = "MATDOC" if family == "S4" else "MSEG"
    assert {l["AUFNR"] for l in tgt.data[lines_t] if l["AUFNR"]} <= loaded_orders
    assert tgt.number_level("PP_ORDER") >= max(int(n) for n in loaded_orders) and tgt.number_level("MM_MBLNR") > 4900000000


@pytest.mark.parametrize("family", FAMILIES)
def test_corrupt_manufacturing_data_is_caught_by_reconciliation_and_held(svc, family):
    s, _ = pair(svc, family)
    src = svc.adapters[s]
    done = next(a for a in src.data["AFPO"] if a["WEMNG"] > 0)["AUFNR"]
    r = next(x for x in src.data["RESB"] if x["AUFNR"] == done)
    src.sim_update("RESB", (done, r["RSPOS"]), BDMNG=r["BDMNG"] + 7)  # reservation no longer matches the BOM explosion
    lines = "MATDOC" if family == "S4" else "MSEG"
    rec = next(x for x in src.data[lines] if x["AUFNR"] == done and x["BWART"] == "101")
    key = (rec["MBLNR"], rec["MJAHR"], rec["ZEILE"])
    src.sim_update(lines, key, MENGE=rec["MENGE"] + 3)  # goods receipt no longer matches the received quantity
    p = mfg_project(svc, family)
    run = run_it(svc, p)
    failed = {c["id"] for c in run.reconciliation["checks"] if c["status"] == "fail"}
    assert {"BUS-PP-COMPONENTS", "BUS-PP-MOVEMENTS"} <= failed and "BUS-PP-STRUCT" not in failed
    assert run.release == "HELD"  # the release gate holds the data back


def test_reconciliation_detects_a_missing_bom_in_the_target(svc):
    p = mfg_project(svc, "ECC", downstream=())
    svc.submit(ALICE, p.id)
    svc.approve(CAROL, p.id)
    run = svc.execute(ALICE, p.id)
    tgt = svc.adapters[svc.tgt_id]
    tgt.data["STKO"] = []  # someone deletes the BOMs afterwards
    tgt._idx.clear()
    from rfactory.reconcile.validator import reconcile
    rec = reconcile(run, p.plan, svc.source_view(p.source_id), tgt, svc.engines[p.id], svc.registries["ECC"], svc.required_sensitive.get(p.id, []))
    assert next(c for c in rec["checks"] if c["id"] == "BUS-PP-BOM")["status"] == "fail"


# ---------------------------------------------------------------- test data (TDM)
def test_manufacturing_templates_are_available_and_not_listed_as_planned(svc):
    t = svc.tdm.templates()
    avail = {x["id"] for x in t["available"]}
    assert {"mfg_order_completed", "mfg_order_open", "md_bom_with_components"} <= avail
    assert "make_to_stock" not in {p["id"] for p in t["planned"]} and {"make_to_order"} <= {p["id"] for p in t["planned"]}


@pytest.mark.parametrize("family", FAMILIES)
def test_candidates_distinguish_completed_and_open_orders(svc, family):
    from rfactory.tdm.templates import TEMPLATES, find_candidates
    s, _ = pair(svc, family)
    src = svc.source_view(s)
    done = find_candidates(TEMPLATES["mfg_order_completed"], src, {"company_code": "1000"})
    open_ = find_candidates(TEMPLATES["mfg_order_open"], src, {"company_code": "1000"})
    assert done and open_ and not ({c["key"] for c in done} & {c["key"] for c in open_})
    assert all(c["attrs"]["goods_movements"] >= 2 for c in done) and all(c["attrs"]["goods_movements"] == 0 for c in open_)


@pytest.mark.parametrize("family", FAMILIES)
def test_tester_gets_manufacturing_data_by_subset_and_by_synthesis(svc, family):
    policy(svc, family)
    bootstrap_masters(svc, family)
    sub = ask(svc, TINA, "mfg_order_completed", family, mode="subset", count=2)
    assert sub["status"] == "FULFILLED", sub
    ds = svc.tdm.get(sub["datasets"][0]["id"])
    assert ds.provenance == "subset" and ds.handles["production_order"] and ds.handles["material_documents"]
    syn = ask(svc, TINA, "mfg_order_completed", family, mode="synthetic", count=2)
    assert syn["status"] == "FULFILLED", syn
    open_ = ask(svc, TINA, "mfg_order_open", family, mode="synthetic", count=1)
    assert open_["status"] == "FULFILLED" and "material_documents" not in svc.tdm.get(open_["datasets"][0]["id"]).handles
    _, t = ids(svc, family)
    tgt = svc.adapters[t]
    for ref in syn["datasets"]:
        did = ref["id"]
        d = svc.tdm.get(did)
        n = d.handles["production_order"]
        assert svc.tdm.verify(did)["intact"]
        o, afko, resb = tgt.get("AUFK", (n,)), tgt.get("AFKO", (n,)), tgt.lookup("RESB", "AUFNR", n)
        assert o["ERNAM"] == "TDM_SYNTH" and tgt.get("STKO", (afko["STLNR"],)) and resb
        lines = tgt.lookup("MATDOC" if family == "S4" else "MSEG", "AUFNR", n)
        assert sum(l["MENGE"] for l in lines if l["BWART"] == "101") == tgt.lookup("AFPO", "AUFNR", n)[0]["WEMNG"]


def test_synthetic_orders_need_materials_in_the_target(svc):
    policy(svc)
    r = ask(svc, TINA, "mfg_order_completed", mode="synthetic", count=1)
    assert r["status"] == "FAILED" and any("two materials" in n for n in r["notes"])


# ---------------------------------------------------------------- delta refresh
def test_delta_refresh_picks_up_a_new_order_and_a_changed_reservation(svc):
    from .test_delta import SCHED
    sc = approved_delta(svc, scopes=[{"scope": Scope(object_type="PRODUCTION_ORDER", company_codes=["1000"])}], include_downstream=["MATERIAL_DOCUMENT"])
    first = svc.delta.run(SCHED, sc.id)
    assert first["status"] == "COMPLETED" and first["new"] >= 10
    src = svc.adapters[svc.src_id]
    prod = next(b for b in src.data["STKO"] if b["WERKS"] == "1000")
    n = "000001099999"
    src.sim_insert("AUFK", {"AUFNR": n, "AUART": "PP01", "ERDAT": REF_DATE.isoformat(), "BUKRS": "1000", "WERKS": "1000", "ERNAM": "BATCHUSR"})
    src.sim_insert("AFKO", {"AUFNR": n, "GAMNG": 12, "GMEIN": "EA", "GSTRP": REF_DATE.isoformat(), "GLTRP": REF_DATE.isoformat(), "STLNR": prod["STLNR"]})
    src.sim_insert("AFPO", {"AUFNR": n, "POSNR": "0001", "MATNR": prod["MATNR"], "PSMNG": 12, "WEMNG": 0, "WERKS": "1000"})
    for k, comp in enumerate(src.lookup("STPO", "STLNR", prod["STLNR"]), start=1):
        src.sim_insert("RESB", {"AUFNR": n, "RSPOS": f"{k:04d}", "MATNR": comp["IDNRK"], "WERKS": "1000", "BDMNG": comp["MENGE"] * 12, "ENMNG": 0})
    second = svc.delta.run(SCHED, sc.id)
    tgt = svc.adapters[svc.tgt_id]
    assert second["new"] == 1 and tgt.get("AUFK", (n,)) is not None and len(tgt.lookup("RESB", "AUFNR", n)) >= 1
    r0 = src.lookup("RESB", "AUFNR", n)[0]
    src.sim_update("RESB", (n, r0["RSPOS"]), BDMNG=r0["BDMNG"])  # touch without changing: nothing to copy
    third = svc.delta.run(SCHED, sc.id)
    assert third["new"] == 0
    comp = src.lookup("RESB", "AUFNR", n)[0]
    src.sim_update("RESB", (n, comp["RSPOS"]), ENMNG=5)
    fourth = svc.delta.run(SCHED, sc.id)
    assert fourth["changed"] == 1 and tgt.get("RESB", (n, comp["RSPOS"]))["ENMNG"] == 5


# ---------------------------------------------------------------- API
def test_manufacturing_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from rfactory.api.main import create_app
    c = TestClient(create_app(tmp_path, persist=False))
    H = lambda u: {"X-Demo-User": u}
    b = c.post("/api/demo/bootstrap", headers=H("root.admin")).json()
    p = c.post("/api/projects", json={"name": "mfg", "source_id": b["source"]["id"], "target_id": b["target"]["id"]}, headers=H("alice.basis")).json()
    m = c.put(f"/api/projects/{p['id']}/manifest", json={"scope": {"object_type": "PRODUCTION_ORDER", "company_codes": ["1000"]}, "include_downstream": ["MATERIAL_DOCUMENT"],
                                                       "masking_policy_id": "gdpr-standard", "conflict_policy": {}}, headers=H("alice.basis"))
    assert m.status_code == 200, m.text
    plan = c.post(f"/api/projects/{p['id']}/plan", headers=H("alice.basis")).json()
    assert plan["by_type"]["PRODUCTION_ORDER"] == 14 and plan["by_type"]["BOM"] >= 1
    inst = c.get(f"/api/projects/{p['id']}/instances", params={"type": "PRODUCTION_ORDER"}, headers=H("alice.basis")).json()
    assert inst["total"] == 14
    assert c.get(f"/api/systems/{b['source']['id']}/discovery", headers=H("alice.basis")).json()["business_objects"]["production_orders"] == 18

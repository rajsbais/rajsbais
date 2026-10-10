"""Routings, order operations and shop-floor confirmations (simplified keys; no work-centre master)."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from rfactory.sap.synthetic import build_source_dataset
from rfactory.selective.manifest import Scope

from .conftest import ALICE, CAROL, ready_project
from .test_manufacturing import FAMILIES, mfg_project, pair, run_it
from .test_tdm import TINA, ask, bootstrap_masters, ids, policy


def src_data(family):
    return build_source_dataset("ECC" if family == "ECC" else "S4")


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_routings_operations_and_confirmations_are_consistent(family):
    d = src_data(family)
    assert d["PLKO"] and d["PLPO"] and d["AFVC"] and d["AFRU"]
    route = {r["PLNNR"]: r for r in d["PLKO"]}
    boms = {b["MATNR"]: b for b in d["STKO"]}
    for gid, r in route.items():
        assert r["MATNR"] in boms and r["WERKS"] == boms[r["MATNR"]]["WERKS"]  # every routing belongs to a product that has a BOM
        assert len([o for o in d["PLPO"] if o["PLNNR"] == gid]) >= 2
    for k in ("PLKO", "PLPO", "AFVC", "AFRU"):
        assert len({tuple(r[f] for f in TABLES[k].keys) for r in d[k]}) == len(d[k])
    for a in d["AFKO"]:
        ops = {o["VORNR"]: o for o in d["AFVC"] if o["AUFNR"] == a["AUFNR"]}
        want = {o["VORNR"]: o for o in d["PLPO"] if o["PLNNR"] == a["PLNNR"]}
        assert set(ops) == set(want) and all(ops[v]["ARBPL"] == want[v]["ARBPL"] for v in ops)  # the order holds a copy of the routing's operations
        pm = next(p for p in d["AFPO"] if p["AUFNR"] == a["AUFNR"])
        confs = [c for c in d["AFRU"] if c["AUFNR"] == a["AUFNR"]]
        if pm["WEMNG"] > 0:
            assert {c["VORNR"] for c in confs} == set(ops)
            assert sum(c["LMNGA"] for c in confs if c["VORNR"] == max(ops)) == pm["WEMNG"]
        else:
            assert not confs
    assert any(n["OBJECT"] == "PP_ROUT" for n in d["NRIV"]) and "PLKO" in NUMBER_RANGE_OBJECTS


@pytest.mark.parametrize("family", FAMILIES)
def test_the_routing_comes_along_with_its_orders_and_the_checks_pass(svc, family):
    p = mfg_project(svc, family)
    types = {i.type for i in p.plan.instances.values()}
    assert "ROUTING" in types
    order = next(i for i in p.plan.instances.values() if i.type == "PRODUCTION_ORDER")
    assert order.rows["AFVC"] and any(r["PLNNR"] for r in order.rows["AFKO"])
    assert any(d.startswith("ROUTING:") for d in order.requires)
    run = run_it(svc, p)
    checks = {c["id"]: c for c in run.reconciliation["checks"]}
    assert checks["BUS-PP-ROUTING"]["status"] == "pass" and checks["BUS-PP-CONFIRM"]["status"] == "pass"
    _, t = pair(svc, family)
    tgt = svc.adapters[t]
    assert tgt.data["PLKO"] and tgt.data["PLPO"] and tgt.data["AFVC"] and tgt.data["AFRU"]
    assert (tgt.number_level("PP_ROUT") or 0) >= max(int(r["PLNNR"]) for r in tgt.data["PLKO"])  # routing numbers are protected like order numbers


def test_a_routing_can_be_refreshed_on_its_own_by_plant_and_material(svc):
    p = ready_project(svc, object_type="ROUTING", company=None, days=0, downstream=(), plants=["1000"])
    types = {i.type for i in p.plan.instances.values()}
    assert types == {"ROUTING", "MATERIAL"} or types >= {"ROUTING"}
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and svc.adapters[svc.tgt_id].data["PLPO"]


def test_reconciliation_catches_a_missing_operation_a_missing_routing_and_a_wrong_yield(svc):
    p = mfg_project(svc, "ECC", downstream=())
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check(cid):
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == cid)
    assert check("BUS-PP-ROUTING")["status"] == "pass"
    done = next(a["AUFNR"] for a in tgt.data["AFPO"] if a["WEMNG"] > 0)
    op = next(o for o in tgt.data["AFVC"] if o["AUFNR"] == done)
    tgt.data["AFVC"].remove(op); tgt._idx.clear()
    assert check("BUS-PP-ROUTING")["status"] == "fail"
    tgt.data["AFVC"].append(op); tgt._idx.clear()
    assert check("BUS-PP-ROUTING")["status"] == "pass"
    last = max(o["VORNR"] for o in tgt.data["AFVC"] if o["AUFNR"] == done)
    conf = next(c for c in tgt.data["AFRU"] if c["AUFNR"] == done and c["VORNR"] == last)
    conf["LMNGA"] += 4; tgt._idx.clear()
    assert check("BUS-PP-CONFIRM")["status"] == "fail"
    conf["LMNGA"] -= 4
    ghost = {**conf, "VORNR": "9990", "RMZHL": "00000009"}
    tgt.data["AFRU"].append(ghost); tgt._idx.clear()
    assert check("BUS-PP-CONFIRM")["status"] == "fail"
    tgt.data["AFRU"].remove(ghost); tgt._idx.clear()
    assert check("BUS-PP-CONFIRM")["status"] == "pass"
    tgt.data["AFRU"] = [c for c in tgt.data["AFRU"] if c["AUFNR"] != done]; tgt._idx.clear()  # received, but nobody confirmed
    assert check("BUS-PP-CONFIRM")["status"] == "fail"
    rt = next(a for a in tgt.data["AFKO"] if a["AUFNR"] == done)["PLNNR"]
    tgt.data["PLKO"] = [r for r in tgt.data["PLKO"] if r["PLNNR"] != rt]; tgt._idx.clear()
    assert check("BUS-PP-ROUTING")["status"] == "fail"


def test_an_order_without_a_routing_is_valid(svc):
    p = mfg_project(svc, "ECC", downstream=())
    src = svc.adapters[svc.src_id]
    # a legacy order: no routing, no operations, no confirmations
    n = "000001099998"
    prod = next(b for b in src.data["STKO"] if b["WERKS"] == "1000")
    from rfactory.sap.synthetic import REF_DATE
    src.sim_insert("AUFK", {"AUFNR": n, "AUART": "PP01", "ERDAT": REF_DATE.isoformat(), "BUKRS": "1000", "WERKS": "1000", "ERNAM": "BATCHUSR"})
    src.sim_insert("AFKO", {"AUFNR": n, "GAMNG": 5, "GMEIN": "EA", "GSTRP": REF_DATE.isoformat(), "GLTRP": REF_DATE.isoformat(), "STLNR": prod["STLNR"], "PLNNR": ""})
    src.sim_insert("AFPO", {"AUFNR": n, "POSNR": "0001", "MATNR": prod["MATNR"], "PSMNG": 5, "WEMNG": 0, "WERKS": "1000"})
    for k, comp in enumerate(src.lookup("STPO", "STLNR", prod["STLNR"]), start=1):
        src.sim_insert("RESB", {"AUFNR": n, "RSPOS": f"{k:04d}", "MATNR": comp["IDNRK"], "WERKS": "1000", "BDMNG": comp["MENGE"] * 5, "ENMNG": 0})
    p = mfg_project(svc, "ECC", downstream=())
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_test_data_includes_routings_operations_and_confirmations(svc, family):
    policy(svc, family)
    bootstrap_masters(svc, family)
    done = ask(svc, TINA, "mfg_order_completed", family, mode="synthetic", count=1)
    open_ = ask(svc, TINA, "mfg_order_open", family, mode="synthetic", count=1)
    assert done["status"] == "FULFILLED" and open_["status"] == "FULFILLED"
    _, t = ids(svc, family)
    tgt = svc.adapters[t]
    n = svc.tdm.get(done["datasets"][0]["id"]).handles["production_order"]
    m = svc.tdm.get(open_["datasets"][0]["id"]).handles["production_order"]
    afko = tgt.get("AFKO", (n,))
    ops, plpo = tgt.lookup("AFVC", "AUFNR", n), tgt.lookup("PLPO", "PLNNR", afko["PLNNR"])
    assert tgt.get("PLKO", (afko["PLNNR"],)) and {o["VORNR"] for o in ops} == {o["VORNR"] for o in plpo} and len(ops) >= 2
    confs = tgt.lookup("AFRU", "AUFNR", n)
    assert {c["VORNR"] for c in confs} == {o["VORNR"] for o in ops}
    assert sum(c["LMNGA"] for c in confs if c["VORNR"] == max(o["VORNR"] for o in ops)) == tgt.lookup("AFPO", "AUFNR", n)[0]["WEMNG"]
    assert tgt.lookup("AFVC", "AUFNR", m) and not tgt.lookup("AFRU", "AUFNR", m)  # the open order has operations but nothing confirmed

"""Plant maintenance orders: they share AUFK with production orders, so the object type is told apart by order type."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import TABLES
from rfactory.sap.synthetic import build_source_dataset

from .test_manufacturing import FAMILIES, pair, run_it
from .test_pm import pm_project


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_maintenance_orders_are_consistent(family):
    d = build_source_dataset(family)
    pm = {o["AUFNR"]: o for o in d["AUFK"] if o["AUART"].startswith("PM")}
    pp = {o["AUFNR"] for o in d["AUFK"] if o["AUART"].startswith("PP")}
    assert pm and pp and not (set(pm) & pp) and len(pm) == len({o["AUFNR"] for o in d["AUFK"]}) - len(pp)
    assert {h["AUFNR"] for h in d["AFIH"]} == set(pm)
    qm = {q["QMNUM"] for q in d["QMEL"]}
    eq = {e["EQUNR"]: e for e in d["EQUI"]}
    assert any(h["QMNUM"] for h in d["AFIH"]) and any(not h["QMNUM"] for h in d["AFIH"])
    for h in d["AFIH"]:
        assert h["EQUNR"] in eq and h["TPLNR"] == eq[h["EQUNR"]]["TPLNR"] and (not h["QMNUM"] or h["QMNUM"] in qm)
        assert pm[h["AUFNR"]]["WERKS"] == eq[h["EQUNR"]]["SWERK"]
    ops = {(o["AUFNR"], o["VORNR"]) for o in d["AFVC"] if o["AUFNR"] in pm}
    assert ops and all((c["AUFNR"], c["VORNR"]) in ops for c in d["AFRU"] if c["AUFNR"] in pm)
    assert any(c["AUFNR"] in pm for c in d["AFRU"])
    assert not [a for a in d["AFKO"] if a["AUFNR"] in pm] and not [a for a in d["AFPO"] if a["AUFNR"] in pm]  # no production-order parts
    assert len({r["RUECK"] for r in d["AFRU"]}) == len(d["AFRU"])
    assert len({tuple(r[k] for k in TABLES["AFIH"].keys) for r in d["AFIH"]}) == len(d["AFIH"])


@pytest.mark.parametrize("family", FAMILIES)
def test_the_two_order_types_never_take_each_others_orders(svc, family):
    pp = pm_project(svc, family, "PRODUCTION_ORDER", plants=["1000"], company=None)
    pms = pm_project(svc, family, "MAINT_ORDER", plants=["1000"], company=None)
    a = {i.key for i in pp.plan.instances.values() if i.type == "PRODUCTION_ORDER"}
    b = {i.key for i in pms.plan.instances.values() if i.type == "MAINT_ORDER"}
    assert a and b and not (a & b)
    src = svc.adapters[pair(svc, family)[0]]
    kind = {o["AUFNR"]: o["AUART"] for o in src.data["AUFK"]}
    assert all(kind[k].startswith("PP") for k in a) and all(kind[k].startswith("PM") for k in b)
    assert not pp.plan.blocking and not pms.plan.blocking


@pytest.mark.parametrize("family", FAMILIES)
def test_orders_bring_notification_equipment_location_and_are_reconciled(svc, family):
    p = pm_project(svc, family, "MAINT_ORDER")
    types = {i.type for i in p.plan.instances.values()}
    assert types >= {"MAINT_ORDER", "EQUIPMENT", "FUNC_LOCATION", "MAINT_NOTIFICATION"}
    for i in p.plan.instances.values():
        if i.type == "MAINT_ORDER":
            h = i.rows["AFIH"][0]
            assert f"EQUIPMENT:{h['EQUNR']}" in i.requires
            assert (f"MAINT_NOTIFICATION:{h['QMNUM']}" in i.requires) == bool(h["QMNUM"])
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-PM-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    tgt = svc.adapters[pair(svc, family)[1]]
    src = svc.adapters[pair(svc, family)[0]]
    assert tgt.data["AFIH"] and all(o["AUART"].startswith("PM") for o in tgt.data["AUFK"] if o["AUFNR"] in {h["AUFNR"] for h in tgt.data["AFIH"]})
    so = {c["RUECK"]: c for c in src.data["AFRU"]}
    for c in tgt.data["AFRU"]:
        if c["RUECK"] in so and so[c["RUECK"]]["AUFNR"] in {h["AUFNR"] for h in tgt.data["AFIH"]}:
            assert c["ERNAM"] != so[c["RUECK"]]["ERNAM"] and c["ISM01"] == so[c["RUECK"]]["ISM01"]  # who confirmed is masked, hours are not
    assert (tgt.number_level("PP_ORDER") or 0) >= max(int(o["AUFNR"]) for o in tgt.data["AUFK"])
    assert run_it(svc, p).release == "RELEASED"


def test_a_notification_pulls_its_orders_downstream(svc):
    p = pm_project(svc, "ECC", "MAINT_NOTIFICATION", downstream=("MAINT_ORDER",))
    orders = [i for i in p.plan.instances.values() if i.type == "MAINT_ORDER"]
    assert orders and all(f"MAINT_NOTIFICATION:{i.rows['AFIH'][0]['QMNUM']}" in i.requires for i in orders)
    plain = pm_project(svc, "ECC", "MAINT_NOTIFICATION")
    assert not [i for i in plain.plan.instances.values() if i.type == "MAINT_ORDER"]


def test_reconciliation_catches_broken_maintenance_orders(svc):
    p = pm_project(svc, "ECC", "MAINT_ORDER")
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-PM-REFS")
    assert check()["status"] == "pass"
    mine = {i.key for i in p.plan.instances.values() if i.type == "MAINT_ORDER"}

    def broken(table, pick):
        keep = list(tgt.data[table])
        tgt.data[table] = [r for r in keep if not pick(r)]; tgt._idx.clear()
        res = check()["status"]
        tgt.data[table] = keep; tgt._idx.clear()
        return res
    first = sorted(mine)[0]
    assert broken("AFIH", lambda r: r["AUFNR"] == first) == "fail"
    assert broken("AFVC", lambda r: r["AUFNR"] in mine) == "fail"
    assert broken("AFRU", lambda r: r["AUFNR"] in mine) == "fail"
    assert broken("AUFK", lambda r: r["AUFNR"] == first) == "fail"
    withq = next(r for r in tgt.data["AFIH"] if r["QMNUM"] and r["AUFNR"] in mine)
    assert broken("QMEL", lambda r: r["QMNUM"] == withq["QMNUM"]) == "fail"
    # an order of the wrong kind in the target
    row = next(r for r in tgt.data["AUFK"] if r["AUFNR"] == first)
    row["AUART"] = "PP01"; tgt._idx.clear()
    assert check()["status"] == "fail"
    row["AUART"] = "PM01"; tgt._idx.clear()
    # a confirmation that refers to an operation the order does not have
    conf = next(r for r in tgt.data["AFRU"] if r["AUFNR"] in mine)
    old = conf["VORNR"]; conf["VORNR"] = "9999"; tgt._idx.clear()
    assert check()["status"] == "fail"
    conf["VORNR"] = old; tgt._idx.clear()
    assert check()["status"] == "pass"


def test_a_reference_to_an_order_of_the_wrong_kind_is_a_dangling_reference(svc):
    from .test_qm import lot_project
    src = svc.adapters[svc.src_id]
    pm_order = next(o["AUFNR"] for o in src.data["AUFK"] if o["AUART"].startswith("PM"))
    lot = next(l for l in src.data["QALS"] if l["WERKS"] == "1000")
    lot["AUFNR"] = pm_order  # a goods-receipt lot that points at a maintenance order
    src._idx.clear()
    p = lot_project(svc, "ECC")
    assert any(i.code == "DANGLING_REFERENCE" for i in p.plan.issues)

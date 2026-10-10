"""Plant maintenance: functional locations, equipment and notifications."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from rfactory.sap.synthetic import build_source_dataset

from .conftest import ready_project
from .test_manufacturing import FAMILIES, pair, run_it

PM = ["IFLOT", "EQUI", "EQKT", "QMEL"]


def pm_project(svc, family, root, **kw):
    s, t = pair(svc, family)
    kw.setdefault("company", None)
    kw.setdefault("days", 0)
    kw.setdefault("downstream", ())
    kw.setdefault("plants", ["1000"])
    return ready_project(svc, src=s, tgt=t, object_type=root, **kw)


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_maintenance_data_is_consistent(family):
    d = build_source_dataset(family)
    assert all(d[t] for t in PM)
    locs = {l["TPLNR"]: l for l in d["IFLOT"]}
    for l in d["IFLOT"]:
        assert not l["TPLMA"] or l["TPLMA"] in locs
    assert sum(1 for l in d["IFLOT"] if not l["TPLMA"]) == len(d["T001W"])  # one top location per plant
    eq = {e["EQUNR"]: e for e in d["EQUI"]}
    assert all(e["TPLNR"] in locs for e in d["EQUI"]) and any(e["MATNR"] for e in d["EQUI"]) and any(not e["MATNR"] for e in d["EQUI"])
    assert {k["EQUNR"] for k in d["EQKT"]} == set(eq)
    for q in d["QMEL"]:
        assert q["EQUNR"] in eq and q["TPLNR"] == eq[q["EQUNR"]]["TPLNR"] and q["SWERK"] == eq[q["EQUNR"]]["SWERK"]
    for t in PM:
        assert len({tuple(r[k] for k in TABLES[t].keys) for r in d[t]}) == len(d[t])
    assert {"EQUI", "QMEL"} <= set(NUMBER_RANGE_OBJECTS) and {"PM_EQUI", "PM_NOTIF"} <= {n["OBJECT"] for n in d["NRIV"]}


@pytest.mark.parametrize("family", FAMILIES)
def test_notifications_bring_their_equipment_and_hierarchy(svc, family):
    p = pm_project(svc, family, "MAINT_NOTIFICATION")
    types = {i.type for i in p.plan.instances.values()}
    assert types >= {"MAINT_NOTIFICATION", "EQUIPMENT", "FUNC_LOCATION"}
    locs = {i.key for i in p.plan.instances.values() if i.type == "FUNC_LOCATION"}
    for i in p.plan.instances.values():
        if i.type == "FUNC_LOCATION":
            sup = i.rows["IFLOT"][0]["TPLMA"]
            assert not sup or sup in locs  # the whole chain up to the plant is pulled in
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-PM-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    so = {q["QMNUM"]: q for q in src.data["QMEL"]}
    assert tgt.data["QMEL"] and {q["QMNUM"] for q in tgt.data["QMEL"]} <= set(so)
    for q in tgt.data["QMEL"]:
        o = so[q["QMNUM"]]
        assert q["ERNAM"] != o["ERNAM"] and q["ERNAM"].startswith("U") and q["QMTXT"] == o["QMTXT"]
    assert (tgt.number_level("PM_NOTIF") or 0) >= max(int(q["QMNUM"]) for q in tgt.data["QMEL"])
    assert (tgt.number_level("PM_EQUI") or 0) >= max(int(e["EQUNR"]) for e in tgt.data["EQUI"])
    assert run_it(svc, p).release == "RELEASED"


def test_equipment_without_a_material_has_no_material_requirement(svc):
    p = pm_project(svc, "ECC", "EQUIPMENT")
    plain = [i for i in p.plan.instances.values() if i.type == "EQUIPMENT" and not i.rows["EQUI"][0]["MATNR"]]
    made = [i for i in p.plan.instances.values() if i.type == "EQUIPMENT" and i.rows["EQUI"][0]["MATNR"]]
    assert plain and made
    assert all(not any(r.startswith("MATERIAL:") for r in i.requires) for i in plain)
    assert all(f"MATERIAL:{i.rows['EQUI'][0]['MATNR']}" in i.requires for i in made)
    assert all(f"FUNC_LOCATION:{i.rows['EQUI'][0]['TPLNR']}" in i.requires for i in plain + made)


def test_a_location_scope_is_limited_to_its_plant(svc):
    p = pm_project(svc, "ECC", "FUNC_LOCATION")
    insts = [i for i in p.plan.instances.values() if i.type == "FUNC_LOCATION"]
    assert insts and all(i.rows["IFLOT"][0]["SWERK"] == "1000" for i in insts)


def test_reconciliation_catches_broken_maintenance_data(svc):
    p = pm_project(svc, "ECC", "MAINT_NOTIFICATION")
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-PM-REFS")
    assert check()["status"] == "pass"
    for table in ("EQKT", "EQUI", "IFLOT"):
        keep = list(tgt.data[table])
        tgt.data[table] = keep[1:]; tgt._idx.clear()
        assert check()["status"] == "fail", table
        tgt.data[table] = keep; tgt._idx.clear()
    assert check()["status"] == "pass"
    mats = list(tgt.data["MARA"])
    tgt.data["MARA"] = []; tgt._idx.clear()
    assert check()["status"] == "fail"  # equipment made from a material needs it
    tgt.data["MARA"] = mats; tgt._idx.clear()
    # a location whose superior is gone
    locs = [dict(l) for l in tgt.data["IFLOT"]]
    child = next(l for l in tgt.data["IFLOT"] if l["TPLMA"])
    child["TPLMA"] = "NO-SUCH-PARENT"; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["IFLOT"] = locs; tgt._idx.clear()
    assert check()["status"] == "pass"


def test_parents_load_before_children_and_a_loop_in_the_hierarchy_is_blocked(svc):
    p = pm_project(svc, "ECC", "FUNC_LOCATION")
    pos = {k: n for n, k in enumerate(p.plan.order)}
    for i in p.plan.instances.values():
        sup = i.rows["IFLOT"][0]["TPLMA"]
        if sup:
            assert pos[f"FUNC_LOCATION:{sup}"] < pos[f"FUNC_LOCATION:{i.key}"]
    # a loop among the instances (the plant location hung below its own station) is found, whatever the type-level graph says
    src = svc.adapters[svc.src_id]
    top = next(l for l in src.data["IFLOT"] if not l["TPLMA"] and l["SWERK"] == "1000")
    leaf = next(l for l in src.data["IFLOT"] if l["FLTYP"] == "S" and l["SWERK"] == "1000")
    top["TPLMA"] = leaf["TPLNR"]
    src._idx.clear()
    p2 = pm_project(svc, "ECC", "FUNC_LOCATION")
    assert any(i.code == "INSTANCE_CYCLE" for i in p2.plan.issues)

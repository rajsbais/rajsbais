"""Warehouse management (WM, ECC and S/4HANA) and embedded EWM (S/4HANA only): bins, quants, transfer and warehouse orders."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from rfactory.sap.synthetic import build_source_dataset

from .conftest import ALICE
from .test_manufacturing import FAMILIES, pair, run_it
from .test_pm import pm_project

WM = ["T300", "LAGP", "LQUA", "LTAK", "LTAP"]
EWM = ["SCWM_LAGP", "SCWM_QUAN", "SCWM_WHO", "SCWM_ORDIM_O"]


def wm_project(svc, family, root, **kw):
    kw.setdefault("plants", ["1000"])
    return pm_project(svc, family, root, **kw)


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_warehouse_data_is_consistent(family):
    d = build_source_dataset(family=family)
    assert all(d[t] for t in WM)
    wh = {w["LGNUM"] for w in d["T300"]}
    bins = {b["LGPLA"]: b for b in d["LAGP"]}
    assert {b["LGNUM"] for b in d["LAGP"]} <= wh and len(bins) == len(d["LAGP"])
    marc = {(m["WERKS"], m["MATNR"]) for m in d["MARC"]}
    for q in d["LQUA"]:
        b = bins[q["LGPLA"]]
        assert q["LGNUM"] == b["LGNUM"] and q["WERKS"] == b["WERKS"] and q["LGTYP"] == b["LGTYP"] and (q["WERKS"], q["MATNR"]) in marc and q["VERME"] > 0
    assert not [q for q in d["LQUA"] if bins[q["LGPLA"]]["LGTYP"] == "902"]  # nothing is stored in the receiving area
    hdr = {h["TANUM"]: h for h in d["LTAK"]}
    for it in d["LTAP"]:
        assert it["TANUM"] in hdr and bins[it["VLPLA"]]["LGNUM"] == hdr[it["TANUM"]]["LGNUM"] == bins[it["NLPLA"]]["LGNUM"]
        assert it["VLPLA"] != it["NLPLA"] and (it["WERKS"], it["MATNR"]) in marc and it["VLTYP"] == bins[it["VLPLA"]]["LGTYP"]
    assert {t["TANUM"] for t in d["LTAK"]} == {i["TANUM"] for i in d["LTAP"]}
    for t in ("LAGP", "LQUA", "LTAK", "LTAP") + (tuple(EWM) if family == "S4" else ()):
        assert len({tuple(r[k] for k in TABLES[t].keys) for r in d[t]}) == len(d[t])
    assert {"LQUA", "LTAK", "SCWM_WHO"} <= set(NUMBER_RANGE_OBJECTS) and {"WM_QUANT", "WM_TO", "EWM_WHO"} <= {n["OBJECT"] for n in d["NRIV"]}
    if family == "ECC":
        assert not any(d[t] for t in EWM) and "E100" not in wh  # embedded EWM exists in S/4HANA only
    else:
        assert all(d[t] for t in EWM) and "E100" in wh
        ebins = {b["LGPLA"]: b for b in d["SCWM_LAGP"]}
        assert all(ebins[q["LGPLA"]]["LGNUM"] == q["LGNUM"] for q in d["SCWM_QUAN"])
        who = {w["WHO"]: w for w in d["SCWM_WHO"]}
        assert all(t["WHO"] in who and ebins[t["VLPLA"]]["LGNUM"] == who[t["WHO"]]["LGNUM"] == ebins[t["NLPLA"]]["LGNUM"] for t in d["SCWM_ORDIM_O"])


def test_ewm_belongs_to_s4_only(svc):
    ecc, s4 = svc.registries["ECC"], svc.registries["S4"]
    assert not {"EWM_BIN", "EWM_WAREHOUSE_ORDER"} & set(ecc.types) and {"EWM_BIN", "EWM_WAREHOUSE_ORDER"} <= set(s4.types)
    assert {"STORAGE_BIN", "TRANSFER_ORDER"} <= set(ecc.types) & set(s4.types)
    assert ecc.validate()["ok"] and s4.validate()["ok"]
    p = wm_project(svc, "ECC", "EWM_BIN")
    assert any(i.code == "UNKNOWN_OBJECT_TYPE" for i in p.plan.issues)  # asking an ECC system for EWM objects is refused, not guessed


@pytest.mark.parametrize("family", FAMILIES)
def test_bins_are_refreshed_with_their_stock_and_reconciled(svc, family):
    p = wm_project(svc, family, "STORAGE_BIN")
    bins = [i for i in p.plan.instances.values() if i.type == "STORAGE_BIN"]
    assert bins and all(i.rows["LAGP"][0]["WERKS"] == "1000" for i in bins) and any(i.rows["LQUA"] for i in bins) and any(not i.rows["LQUA"] for i in bins)
    assert {i.type for i in p.plan.instances.values()} >= {"STORAGE_BIN", "MATERIAL"}
    assert {c for i in bins for c in i.configs if c.startswith("WAREHOUSE:")} == {"WAREHOUSE:100"}  # the warehouse number is customizing the target must already hold
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    assert {c["id"]: c["status"] for c in run.reconciliation["checks"]}["BUS-WM-REFS"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    assert {b["LGPLA"] for b in tgt.data["LAGP"]} == {b["LGPLA"] for b in src.data["LAGP"] if b["WERKS"] == "1000"}
    assert sum(q["VERME"] for q in tgt.data["LQUA"]) == sum(q["VERME"] for q in src.data["LQUA"] if q["WERKS"] == "1000")  # stock is copied exactly, never masked
    assert (tgt.number_level("WM_QUANT") or 0) >= max(int(q["LQNUM"]) for q in tgt.data["LQUA"])
    assert run_it(svc, p).release == "RELEASED"


@pytest.mark.parametrize("family", FAMILIES)
def test_transfer_orders_bring_their_bins_and_materials_and_mask_the_creator(svc, family):
    p = wm_project(svc, family, "TRANSFER_ORDER")
    types = {i.type for i in p.plan.instances.values()}
    assert types >= {"TRANSFER_ORDER", "STORAGE_BIN", "MATERIAL"}
    for i in p.plan.instances.values():
        if i.type == "TRANSFER_ORDER":
            for it in i.rows["LTAP"]:
                assert f"STORAGE_BIN:{it['VLPLA']}" in i.requires and f"STORAGE_BIN:{it['NLPLA']}" in i.requires
    run = run_it(svc, p)
    assert run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-WM-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    so = {h["TANUM"]: h for h in svc.adapters[s].data["LTAK"]}
    tgt = svc.adapters[t]
    assert tgt.data["LTAK"] and all(h["QNAME"] != so[h["TANUM"]]["QNAME"] and h["QNAME"].startswith("U") and h["BWLVS"] == so[h["TANUM"]]["BWLVS"] for h in tgt.data["LTAK"])
    assert (tgt.number_level("WM_TO") or 0) >= max(int(h["TANUM"]) for h in tgt.data["LTAK"])


def test_a_bin_pulls_the_transfer_orders_that_touch_it_only_when_asked(svc):
    p = wm_project(svc, "ECC", "STORAGE_BIN", downstream=("TRANSFER_ORDER",))
    orders = [i for i in p.plan.instances.values() if i.type == "TRANSFER_ORDER"]
    assert orders and all(f"STORAGE_BIN:{it['VLPLA']}" in i.requires for i in orders for it in i.rows["LTAP"])
    assert not wm_project(svc, "ECC", "STORAGE_BIN").plan.blocking
    assert not [i for i in wm_project(svc, "ECC", "STORAGE_BIN").plan.instances.values() if i.type == "TRANSFER_ORDER"]


def test_the_plant_scope_limits_warehouses(svc):
    p = wm_project(svc, "ECC", "STORAGE_BIN", plants=["1010"])
    bins = [i for i in p.plan.instances.values() if i.type == "STORAGE_BIN"]
    assert bins and {i.rows["LAGP"][0]["LGNUM"] for i in bins} == {"110"}


def test_a_warehouse_missing_in_the_target_is_customizing_the_platform_never_copies(svc):
    tgt = svc.adapters[svc.tgt_id]
    p = wm_project(svc, "ECC", "STORAGE_BIN")
    assert any(c.startswith("WAREHOUSE:") for i in p.plan.instances.values() for c in i.configs)
    tgt.data["T300"] = [w for w in tgt.data["T300"] if w["LGNUM"] != "100"]
    tgt._idx.clear()
    svc.analyze_conflicts(ALICE, p.id)
    found = [f for f in p.report.findings if f.type.value == "CONFIG_MISSING"]
    assert found and all("WAREHOUSE:100" in f.message for f in found)  # the bins are held back, naming the warehouse number the target lacks


@pytest.mark.parametrize("what", ["quant gone", "quant changed", "bin gone", "order gone", "item gone", "item bin gone", "item destination points nowhere", "item source points nowhere", "material gone"])
def test_reconciliation_catches_broken_warehouse_data(svc, what):
    p = wm_project(svc, "ECC", "TRANSFER_ORDER")
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-WM-REFS")["status"]
    assert check() == "pass"
    used = {i["VLPLA"] for i in tgt.data["LTAP"]}
    if what in ("quant gone", "quant changed"):
        # quants belong to bins, so reconcile them through a bin refresh
        pb = wm_project(svc, "ECC", "STORAGE_BIN")
        run = run_it(svc, pb)
        p, view = pb, svc.source_view(pb.source_id)
        assert check() == "pass"
        q = next(x for x in tgt.data["LQUA"] if f"STORAGE_BIN:{x['LGPLA']}" in run.loaded)  # a bin this run loaded (identical earlier ones are skipped, not re-checked)
        if what == "quant gone":
            tgt.data["LQUA"] = [x for x in tgt.data["LQUA"] if x is not q]
        else:
            q["VERME"] = q["VERME"] + 1
    elif what == "bin gone":
        tgt.data["LAGP"] = [b for b in tgt.data["LAGP"] if b["LGPLA"] not in used]
    elif what == "order gone":
        tgt.data["LTAK"] = tgt.data["LTAK"][1:]
    elif what == "item gone":
        tgt.data["LTAP"] = tgt.data["LTAP"][1:]
    elif what == "item bin gone":
        tgt.data["LAGP"] = [b for b in tgt.data["LAGP"] if b["LGPLA"] != tgt.data["LTAP"][0]["NLPLA"]]
    elif what == "item destination points nowhere":
        tgt.data["LTAP"][0]["NLPLA"] = "NO-SUCH-BIN"
    elif what == "item source points nowhere":
        tgt.data["LTAP"][0]["VLPLA"] = "NO-SUCH-BIN"
    elif what == "material gone":
        tgt.data["MARA"] = []
    tgt._idx.clear()
    assert check() == "fail", what


def test_ewm_in_s4hana(svc):
    p = wm_project(svc, "S4", "EWM_WAREHOUSE_ORDER")
    types = {i.type for i in p.plan.instances.values()}
    assert types >= {"EWM_WAREHOUSE_ORDER", "EWM_BIN", "MATERIAL"}
    run = run_it(svc, p)
    assert run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-EWM-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and "BUS-WM-REFS" not in checks
    s, t = pair(svc, "S4")
    so = {w["WHO"]: w for w in svc.adapters[s].data["SCWM_WHO"]}
    tgt = svc.adapters[t]
    assert tgt.data["SCWM_WHO"] and all(w["CREATED_BY"] != so[w["WHO"]]["CREATED_BY"] and w["CREATED_BY"].startswith("U") for w in tgt.data["SCWM_WHO"])
    assert (tgt.number_level("EWM_WHO") or 0) >= max(int(w["WHO"]) for w in tgt.data["SCWM_WHO"])
    pb = wm_project(svc, "S4", "EWM_BIN")
    runb = run_it(svc, pb)
    assert runb.release == "RELEASED" and {c["id"]: c["status"] for c in runb.reconciliation["checks"]}["BUS-EWM-REFS"] == "pass"
    assert sum(q["QUAN"] for q in tgt.data["SCWM_QUAN"]) == sum(q["QUAN"] for q in svc.adapters[s].data["SCWM_QUAN"] if q["WERKS"] == "1000")
    # the classic WM tables of the same system are untouched by an EWM refresh, and the EWM check fails when its stock is wrong
    view, reg = svc.source_view(pb.source_id), svc.registries["S4"]

    def check():
        rec = reconcile(runb, pb.plan, view, tgt, svc.engines[pb.id], reg, svc.required_sensitive.get(pb.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-EWM-REFS")["status"]
    assert check() == "pass"
    q = next(x for x in tgt.data["SCWM_QUAN"] if f"EWM_BIN:{x['LGPLA']}" in runb.loaded)
    q["QUAN"] += 5
    tgt._idx.clear()
    assert check() == "fail"


def test_every_bin_reference_of_an_order_can_pull_the_order_in(svc):
    for fam in ("ECC", "S4"):
        reg = svc.registries[fam]
        refs = [r for r in reg.relationships if r.src in ("TRANSFER_ORDER", "EWM_WAREHOUSE_ORDER") and r.dst in ("STORAGE_BIN", "EWM_BIN")]
        assert len(refs) == (2 if fam == "ECC" else 4) and all(r.reverse for r in refs)  # source AND destination bin: an order touching a selected bin on either side
        assert {r.via_field for r in refs} >= {"VLPLA", "NLPLA"}

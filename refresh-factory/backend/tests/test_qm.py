"""Quality management: inspection lots with characteristics, results and usage decisions."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from rfactory.sap.synthetic import build_source_dataset

from .conftest import ALICE, CAROL, ready_project
from .test_manufacturing import FAMILIES, pair, run_it

QM = ["QALS", "QAMV", "QASR", "QAVE"]


def lot_project(svc, family, root="INSPECTION_LOT", **kw):
    s, t = pair(svc, family)
    kw.setdefault("company", None)
    kw.setdefault("days", 0)
    kw.setdefault("downstream", ())
    kw.setdefault("plants", ["1000"])
    return ready_project(svc, src=s, tgt=t, object_type=root, **kw)


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_inspection_data_is_consistent(family):
    d = build_source_dataset(family)
    assert all(d[t] for t in QM)
    done = {a["AUFNR"] for a in d["AFPO"] if a["WEMNG"] > 0}
    assert {l["AUFNR"] for l in d["QALS"]} == done  # one lot per completed order
    chars = {(c["PRUEFLOS"], c["MERKNR"]): c for c in d["QAMV"]}
    for r in d["QASR"]:
        assert (r["PRUEFLOS"], r["MERKNR"]) in chars
    for l in d["QALS"]:
        assert len([v for v in d["QAVE"] if v["PRUEFLOS"] == l["PRUEFLOS"]]) == 1
        res = [r for r in d["QASR"] if r["PRUEFLOS"] == l["PRUEFLOS"]]
        inside = all(chars[(r["PRUEFLOS"], r["MERKNR"])]["TOLUNL"] <= r["MESSWERT"] <= chars[(r["PRUEFLOS"], r["MERKNR"])]["TOLOBL"] for r in res)
        assert next(v for v in d["QAVE"] if v["PRUEFLOS"] == l["PRUEFLOS"])["VCODE"] == ("A" if inside else "R")
    for t in QM:
        assert len({tuple(r[k] for k in TABLES[t].keys) for r in d[t]}) == len(d[t])
    assert "QALS" in NUMBER_RANGE_OBJECTS and any(n["OBJECT"] == "QM_LOT" for n in d["NRIV"])


@pytest.mark.parametrize("family", FAMILIES)
def test_lots_are_refreshed_masked_and_reconciled(svc, family):
    p = lot_project(svc, family)
    types = {i.type for i in p.plan.instances.values()}
    assert types >= {"INSPECTION_LOT", "MATERIAL"}
    lot = next(i for i in p.plan.instances.values() if i.type == "INSPECTION_LOT")
    assert lot.rows["QAMV"] and lot.rows["QASR"] and lot.rows["QAVE"]
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-QM-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    want = {l["PRUEFLOS"] for l in src.data["QALS"] if l["WERKS"] == "1000"}
    assert {l["PRUEFLOS"] for l in tgt.data["QALS"]} == want
    so = {(r["PRUEFLOS"], r["MERKNR"]): r for r in src.data["QASR"]}
    for r in tgt.data["QASR"]:
        o = so[(r["PRUEFLOS"], r["MERKNR"])]
        assert r["PRUEFER"] != o["PRUEFER"] and r["PRUEFER"].startswith("U") and r["MESSWERT"] == o["MESSWERT"]  # people masked, measurements untouched
    for v in tgt.data["QAVE"]:
        assert v["VAENAME"].startswith("U") and v["VCODE"] in ("A", "R")
    assert (tgt.number_level("QM_LOT") or 0) >= max(int(l["PRUEFLOS"]) for l in tgt.data["QALS"])


def test_downstream_lots_follow_their_production_orders(svc):
    p = ready_project(svc, src=svc.src_id, tgt=svc.tgt_id, object_type="PRODUCTION_ORDER", company="1000", days=0, downstream=("INSPECTION_LOT",))
    lots = [i for i in p.plan.instances.values() if i.type == "INSPECTION_LOT"]
    orders = {i.key for i in p.plan.instances.values() if i.type == "PRODUCTION_ORDER"}
    assert lots and all(l.rows["QALS"][0]["AUFNR"] in orders for l in lots)
    assert all(f"PRODUCTION_ORDER:{l.rows['QALS'][0]['AUFNR']}" in l.requires for l in lots)
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED"
    assert all(c["status"] == "pass" for c in run.reconciliation["checks"] if c["id"].startswith(("BUS-QM", "BUS-PP")))


def test_a_lot_without_a_production_order_has_no_order_requirement(svc):
    src = svc.adapters[svc.src_id]
    base = next(l for l in src.data["QALS"] if l["WERKS"] == "1000")
    src.sim_insert("QALS", {**base, "PRUEFLOS": "000199999999", "ART": "01", "AUFNR": ""})  # goods-receipt lot for a purchase order
    src.sim_insert("QAVE", {"PRUEFLOS": "000199999999", "VCODE": "A", "VDATUM": base["ENSTEHDAT"], "VAENAME": "QA0001"})
    p = lot_project(svc, "ECC")
    lot = p.plan.instances["INSPECTION_LOT:000199999999"]
    assert not any(r.startswith("PRODUCTION_ORDER:") for r in lot.requires) and not lot.rows["QAMV"]  # a lot without characteristics is valid
    run = run_it(svc, p)
    assert run.release == "RELEASED"


def test_reconciliation_catches_incomplete_lots_and_orphan_results(svc):
    p = lot_project(svc, "ECC")
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-QM-REFS")
    assert check()["status"] == "pass"
    res = list(tgt.data["QASR"])
    tgt.data["QASR"] = res[1:]; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["QASR"] = res; tgt._idx.clear()
    chars = list(tgt.data["QAMV"])
    tgt.data["QAMV"] = chars[1:]; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["QAMV"] = chars; tgt._idx.clear()
    dec = list(tgt.data["QAVE"])
    tgt.data["QAVE"] = []; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["QAVE"] = dec; tgt._idx.clear()
    assert check()["status"] == "pass"
    mats = list(tgt.data["MARA"])
    tgt.data["MARA"] = []; tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["MARA"] = mats; tgt._idx.clear()
    orders = list(tgt.data["AUFK"])
    if any(l["AUFNR"] for l in tgt.data["QALS"]):
        tgt.data["AUFK"] = []; tgt._idx.clear()
        assert check()["status"] == "fail"
        tgt.data["AUFK"] = orders; tgt._idx.clear()

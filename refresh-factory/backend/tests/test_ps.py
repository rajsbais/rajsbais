"""Project system: projects with their WBS hierarchy and actual costs."""
import pytest

from rfactory.reconcile.validator import reconcile
from rfactory.sap.ddic import TABLES
from rfactory.sap.synthetic import build_source_dataset

from .test_manufacturing import FAMILIES, pair, run_it
from .test_pm import pm_project

PS = ["PROJ", "PRPS", "COSP"]


@pytest.mark.parametrize("family", FAMILIES)
def test_synthetic_project_data_is_consistent(family):
    d = build_source_dataset(family)
    assert all(d[t] for t in PS)
    wbs = {w["POSID"]: w for w in d["PRPS"]}
    proj = {p["PSPID"] for p in d["PROJ"]}
    for w in d["PRPS"]:
        assert w["PSPID"] in proj
        assert (w["POSID_UP"] == "") == (w["STUFE"] == 1)
        if w["POSID_UP"]:
            assert wbs[w["POSID_UP"]]["PSPID"] == w["PSPID"] and wbs[w["POSID_UP"]]["STUFE"] == w["STUFE"] - 1
    assert {p["PSPID"] for p in d["PROJ"]} == {w["POSID"] for w in d["PRPS"] if w["STUFE"] == 1}  # one root element per project
    parents = {w["POSID_UP"] for w in d["PRPS"]}
    assert all(c["POSID"] in wbs and c["POSID"] not in parents for c in d["COSP"])  # costs sit on leaf elements only
    assert {p["WERKS"] for p in d["PROJ"]} == {p["WERKS"] for p in d["T001W"]}
    for t in PS:
        assert len({tuple(r[k] for k in TABLES[t].keys) for r in d[t]}) == len(d[t])


@pytest.mark.parametrize("family", FAMILIES)
def test_projects_are_refreshed_masked_and_reconciled(svc, family):
    p = pm_project(svc, family, "PROJECT", company="1000", plants=[])
    projs = [i for i in p.plan.instances.values() if i.type == "PROJECT"]
    assert projs and all(i.rows["PRPS"] and i.rows["COSP"] for i in projs)
    run = run_it(svc, p)
    assert run.status == "COMPLETED" and run.release == "RELEASED", [c for c in run.reconciliation["checks"] if c["status"] == "fail"]
    checks = {c["id"]: c["status"] for c in run.reconciliation["checks"]}
    assert checks["BUS-PS-REFS"] == "pass" and checks["SEC-RESIDUAL"] == "pass" and checks["SEC-MASK-COVERAGE"] == "pass"
    s, t = pair(svc, family)
    src, tgt = svc.adapters[s], svc.adapters[t]
    so = {w["POSID"]: w for w in src.data["PRPS"]}
    assert tgt.data["PRPS"]
    for w in tgt.data["PRPS"]:
        o = so[w["POSID"]]
        assert w["ERNAM"] != o["ERNAM"] and w["ERNAM"].startswith("U") and w["POST1"] == o["POST1"] and w["POSID_UP"] == o["POSID_UP"]
    for pr in tgt.data["PROJ"]:
        assert pr["ERNAM"].startswith("U")
    sc = {(c["POSID"], c["GJAHR"], c["KSTAR"]): c["WKGBTR"] for c in src.data["COSP"]}
    assert tgt.data["COSP"] and all(sc[(c["POSID"], c["GJAHR"], c["KSTAR"])] == c["WKGBTR"] for c in tgt.data["COSP"])  # money is not masked
    assert run_it(svc, p).release == "RELEASED"


def test_a_project_scope_is_limited_to_its_company_code(svc):
    p = pm_project(svc, "ECC", "PROJECT", company="1000", plants=[])
    projs = [i for i in p.plan.instances.values() if i.type == "PROJECT"]
    assert projs and all(i.rows["PROJ"][0]["VBUKR"] == "1000" for i in projs)
    assert {i.rows["PROJ"][0]["WERKS"] for i in projs} == {"1000", "1010"}  # by company code, so every plant of the company (company 1000 owns two plants)


def test_reconciliation_catches_broken_projects(svc):
    p = pm_project(svc, "ECC", "PROJECT", company="1000", plants=[])
    run = run_it(svc, p)
    tgt = svc.adapters[svc.tgt_id]
    view, reg = svc.source_view(p.source_id), svc.registries["ECC"]

    def check():
        rec = reconcile(run, p.plan, view, tgt, svc.engines[p.id], reg, svc.required_sensitive.get(p.id, []))
        return next(c for c in rec["checks"] if c["id"] == "BUS-PS-REFS")
    assert check()["status"] == "pass"

    def broken(table, pick):
        keep = list(tgt.data[table])
        tgt.data[table] = [r for r in keep if not pick(r)]; tgt._idx.clear()
        res = check()["status"]
        tgt.data[table] = keep; tgt._idx.clear()
        return res
    first = tgt.data["PROJ"][0]["PSPID"]
    assert broken("PROJ", lambda r: r["PSPID"] == first) == "fail"
    leaf = next(w["POSID"] for w in tgt.data["PRPS"] if w["STUFE"] == 3 or w["STUFE"] == 2)
    assert broken("PRPS", lambda r: r["POSID"] == leaf) == "fail"
    assert broken("COSP", lambda r: r is tgt.data["COSP"][0]) == "fail"
    # a changed amount (same number of rows) is caught by the cost totals
    c = tgt.data["COSP"][0]
    old = c["WKGBTR"]; c["WKGBTR"] = old + 1.0; tgt._idx.clear()
    assert check()["status"] == "fail"
    c["WKGBTR"] = old; tgt._idx.clear()
    # an element in the target that was never loaded (stale data under the same project)
    tgt.data["PRPS"].append({"POSID": first + ".STALE", "PSPID": first, "POST1": "x", "POSID_UP": first, "STUFE": 2, "PBUKR": "1000", "WERKS": "1000", "ERNAM": "U1"}); tgt._idx.clear()
    assert check()["status"] == "fail"
    tgt.data["PRPS"].pop(); tgt._idx.clear()
    # a WBS element whose superior is gone
    w = next(r for r in tgt.data["PRPS"] if r["POSID_UP"])
    up = w["POSID_UP"]; w["POSID_UP"] = "NOPE"; tgt._idx.clear()
    assert check()["status"] == "fail"
    w["POSID_UP"] = up; tgt._idx.clear()
    assert check()["status"] == "pass"

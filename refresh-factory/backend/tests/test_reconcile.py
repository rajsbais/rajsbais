"""Reconciliation must actually *detect* damage: each test corrupts the target after a good run."""
import pytest

from .conftest import ALICE, approved_project


@pytest.fixture
def done(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    assert run.reconciliation["release"] == "RELEASED"
    return svc, p, run, svc.adapters[svc.tgt_id]


def rerun(svc, p, run):
    svc._after_run(p, run)
    return {c["id"]: c for c in run.reconciliation["checks"]}, run.release


def loaded_of(p, run, prefix):
    return next(i for i in run.loaded if i.startswith(prefix))


def test_all_three_categories_pass_on_clean_run(done):
    svc, p, run, tgt = done
    checks, rel = rerun(svc, p, run)
    assert rel == "RELEASED" and all(c["status"] == "pass" for c in checks.values())
    assert len(checks) >= 25


def test_detects_missing_item_row(done):
    svc, p, run, tgt = done
    o = loaded_of(p, run, "SALES_ORDER")
    key = p.plan.instances[o].rows["VBAP"][0]
    tgt.delete("VBAP", (key["VBELN"], key["POSNR"]))
    checks, rel = rerun(svc, p, run)
    assert rel == "HELD" and checks["TECH-COUNT-VBAP"]["status"] == "fail" and checks["BUS-TOTALS"]["status"] == "fail"
    assert p.status == "HELD"


def test_detects_checksum_drift_and_unbalanced_fi_document(done):
    svc, p, run, tgt = done
    fi = loaded_of(p, run, "FI_DOCUMENT")
    seg = p.plan.instances[fi].rows["BSEG"][0]
    tgt.get("BSEG", (seg["BUKRS"], seg["BELNR"], seg["GJAHR"], seg["BUZEI"]))["DMBTR"] += 5
    checks, rel = rerun(svc, p, run)
    assert checks["TECH-CHECKSUM"]["status"] == "fail"
    assert checks["BUS-FI-BALANCE"]["status"] == "fail" and checks["BUS-FI-BILLING"]["status"] == "fail"
    assert rel == "HELD"


def test_detects_broken_document_flow(done):
    svc, p, run, tgt = done
    d = loaded_of(p, run, "DELIVERY")
    order = p.plan.instances[d].rows["LIPS"][0]["VGBEL"]
    for r in tgt.lookup("VBAP", "VBELN", order):
        tgt.delete("VBAP", (r["VBELN"], r["POSNR"]))
    tgt.delete("VBAK", (order,))
    checks, rel = rerun(svc, p, run)
    assert checks["BUS-CHAIN"]["status"] == "fail" and checks["BUS-DOCFLOW"]["status"] == "fail" and rel == "HELD"


def test_detects_missing_master_and_dangling_reference(done):
    svc, p, run, tgt = done
    o = loaded_of(p, run, "SALES_ORDER")
    cust = p.plan.instances[o].rows["VBAK"][0]["KUNNR"]
    for t in ("KNB1", "KNVV"):
        for r in tgt.lookup(t, "KUNNR", cust):
            tgt.delete(t, tuple(r[k] for k in __import__("rfactory.sap.ddic", fromlist=["TABLES"]).TABLES[t].keys))
    tgt.delete("KNA1", (cust,))
    checks, rel = rerun(svc, p, run)
    assert checks["BUS-MASTER"]["status"] == "fail" and checks["TECH-REFS"]["status"] == "fail" and rel == "HELD"


def test_detects_unmasked_value_residual_pii_and_blocks_release(done):
    svc, p, run, tgt = done
    cust = next(i for i in run.loaded if i.startswith("CUSTOMER")).split(":")[1]
    src = svc.adapters[svc.src_id].get("KNA1", (cust,))
    tgt.get("KNA1", (cust,))["NAME1"] = src["NAME1"]
    tgt.get("KNA1", (cust,))["TELF1"] = src["TELF1"]
    checks, rel = rerun(svc, p, run)
    assert checks["SEC-RESIDUAL"]["status"] == "fail" and rel == "HELD"


def test_detects_pii_in_unprotected_custom_field(svc):
    p = approved_project(svc)
    run = svc.execute(ALICE, p.id)
    p.masking_policy.rules = [r for r in p.masking_policy.rules if r.field != "ZZ_CONTACT_EMAIL"]
    svc.engines[p.id].policy.rules = p.masking_policy.rules
    checks, rel = rerun(svc, p, run)
    assert checks["SEC-PII-SCAN"]["status"] == "fail" and checks["SEC-MASK-COVERAGE"]["status"] == "fail" and rel == "HELD"


def test_detects_active_outbound_interface(done):
    svc, p, run, tgt = done
    tgt.outbound_interfaces()[0]["active"] = True
    checks, rel = rerun(svc, p, run)
    assert checks["SEC-OUTBOUND"]["status"] == "fail" and rel == "HELD"


def test_detects_number_range_regression_and_duplicates(done):
    svc, p, run, tgt = done
    tgt.set_number_level("SD_ORDER", 5000001)
    o = loaded_of(p, run, "SALES_ORDER")
    tgt.data["VBAK"].append(dict(tgt.get("VBAK", (p.plan.instances[o].key,))))
    tgt._idx.clear()
    checks, rel = rerun(svc, p, run)
    assert checks["BUS-NUMBER-RANGE"]["status"] == "fail" and checks["TECH-DUP"]["status"] == "fail" and rel == "HELD"

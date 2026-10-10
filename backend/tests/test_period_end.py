"""Period-end reconciliation: the target's trial balance per fiscal period, the period cut-off against the
transferred documents, open items in a foreign currency (document-currency amounts agree, local amounts may be
revalued), valuation per area and the price control of the transferred materials; from rows, from Universal
Journal aggregates, and the honest answer where a path cannot know."""
from collections import defaultdict

from sqlalchemy import select

from sdtf.catalog.store import RecordStore
from sdtf.models import ReconciliationResult
from sdtf.reconciliation import service as svc
from sdtf.reconciliation import views
from sdtf.runtime import rfc


def _rows(session, run_id, name):
    return [r for r in session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id, ReconciliationResult.check_name == name)).scalars().all()]


def test_slice_reconciles_the_period_end_from_rows(session, slice_result):
    rid = slice_result["run_id"]
    per = _rows(session, rid, "period_trial_balance")
    assert per and all(r.status == "PASS" for r in per) and all(r.subject.count("/") == 2 and r.evidence["documents"] > 0 for r in per)
    cut = _rows(session, rid, "period_cutoff")
    assert len(cut) == 1 and cut[0].status == "PASS" and "all within the source's" in cut[0].explanation and cut[0].source_value == cut[0].target_value and cut[0].evidence["target_periods"] == len(per) and cut[0].evidence["transferred_periods_verified"]
    fx = _rows(session, rid, "fx_open_items")
    assert fx and all(r.status == "PASS" for r in fx) and any(r.subject.endswith("/USD") or r.subject.endswith("/EUR") for r in fx)
    foreign = next(r for r in fx if "/" in r.subject)
    assert "open item(s) in" in foreign.explanation and foreign.source_value == foreign.target_value and foreign.evidence["local_variance"] == 0
    areas = _rows(session, rid, "inventory_valuation_by_area")
    assert len(areas) >= 2 and all(r.status == "PASS" and r.source_value == r.target_value for r in areas)
    pc = _rows(session, rid, "material_price_control")
    assert len(pc) == len(areas) and all(r.status == "PASS" and "same price control and price" in r.explanation for r in pc)


def test_universal_journal_aggregates_carry_period_and_currency(session, slice_result):
    store = RecordStore.load(session, slice_result["source_id"])
    rows = {t: list(store.rows(t)) for t in ("BKPF", "BSEG", "BSID", "BSIK", "ANLC", "MBEW", "T001K", "T001")}
    rows["ACDOCA"] = views.acdoca_from_journal(rows["BKPF"], rows["BSEG"])
    s4 = RecordStore.from_tables("s4", rows)
    client = rfc.AbapAddonClient(rfc.SimulatedAbapAddon(s4, snapshot_ttl=60))
    client.open_snapshot()
    areas = sorted({r["BWKEY"] for r in rows["T001K"] if r["BUKRS"] == "5000"})
    A = views.journal_aggregates(client, ["5000"], "ACDOCA", "0L", areas)
    B = views.journal_aggregates(client, ["5000"], "BSEG", "0L", areas)
    assert B["periods"] is None and B["open_fx"] is None and A["periods"] and A["open_fx"] is not None and A["documents_by_period"] and A["price_control"]
    heads = {(h["BUKRS"], h["BELNR"], str(h["GJAHR"])): h for h in rows["BKPF"]}
    expect: dict = defaultdict(float)
    for l in rows["BSEG"]:
        if l["BUKRS"] == "5000":
            h = heads[(l["BUKRS"], l["BELNR"], str(l["GJAHR"]))]
            expect[(str(l["GJAHR"]), str(h["MONAT"]).zfill(3), l["SHKZG"])] += float(l["DMBTR"])
    got = {(str(a["GJAHR"]), str(a["MONAT"]).zfill(3), a["SHKZG"]): round(float(a["SUM_DMBTR"]), 2) for a in A["periods"]}
    assert got == {k: round(v, 2) for k, v in expect.items()}
    cur = next(r["WAERS"] for r in rows["T001"] if r["BUKRS"] == "5000")
    fx_lines = [l for l in rows["BSEG"] if l["BUKRS"] == "5000" and l["KOART"] in ("D", "K") and not l.get("AUGBL") and heads[(l["BUKRS"], l["BELNR"], str(l["GJAHR"]))]["WAERS"] != cur]
    assert fx_lines, "the synthetic landscape bills some invoices in a foreign currency"
    fx = [a for a in A["open_fx"] if a["WAERS"] != cur]
    assert sum(int(a["COUNT"]) for a in fx) == len(fx_lines) and round(sum((1 if a["SHKZG"] == "S" else -1) * float(a["SUM_WRBTR"]) for a in fx), 2) == round(sum((1 if l["SHKZG"] == "S" else -1) * float(l["WRBTR"]) for l in fx_lines), 2)
    assert {(str(a["BWKEY"]), a["VPRSV"]) for a in A["price_control"]} <= {(r["BWKEY"], r["VPRSV"]) for r in rows["MBEW"]}


def _bal(periods, fx, areas, held=None, prices=None, cur=None, periods_all=None):
    b = svc.SourceBalances()
    b.periods, b.fx_open, b.cc_currency = periods, fx, cur or {"SP01": "EUR"}
    b.periods_all = periods_all if periods_all is not None else (set(periods) if periods else None)
    b.inventory_by_area, b.held_by_area = defaultdict(float, areas), defaultdict(float, held or {})
    b.price_control = prices if prices is not None else {}
    return b


def _tb(periods, fx, areas, prices=None, mode="rows", price_counts=None, periods_all=None):
    t = svc.TargetBalances()
    t.periods, t.fx_open, t.inventory_by_area, t.price_control, t.price_counts, t.mode = periods, fx, defaultdict(float, areas), prices, price_counts, mode
    t.periods_all = periods_all if periods_all is not None else (set(periods) if periods else None)
    return t


def test_period_end_checks_name_every_deviation():
    P = {("SP01", "2025", "001"): {"debit": 100.0, "credit": 100.0, "documents": {"a"}}, ("SP01", "2025", "002"): {"debit": 50.0, "credit": 50.0, "documents": {"b"}}}
    FX = {("SP01", "USD"): {"count": 2, "wrbtr": 110.0, "dmbtr": 100.0}}
    AR = {"SP10": 1000.0, "SP20": 500.0}
    PR = {("M1", "SP10"): ("S", 10.0), ("M2", "SP20"): ("V", 7.5)}
    ok = svc.period_end_checks("r", [{}], [_bal(P, FX, AR, prices=PR)], _tb(P, FX, AR, PR), {"SP01"}, False, "")
    by = defaultdict(list)
    for r in ok:
        by[r.check_name].append(r)
    assert [r.status for r in by["period_trial_balance"]] == ["PASS", "PASS"] and by["period_cutoff"][0].status == "PASS" and "2 period(s) posted in the target, all within the source's; last period 2025/002" in by["period_cutoff"][0].explanation
    assert by["fx_open_items"][0].status == "PASS" and [r.status for r in by["inventory_valuation_by_area"]] == ["PASS", "PASS"] and [r.status for r in by["material_price_control"]] == ["PASS", "PASS"]
    # an unbalanced period, a period the source never transferred, a missing one, revalued local amounts, a held area, a price change
    TP = {("SP01", "2025", "001"): {"debit": 100.0, "credit": 90.0, "documents": {"a"}}, ("SP01", "2025", "003"): {"debit": 5.0, "credit": 5.0, "documents": {"c"}}}
    TFX = {("SP01", "USD"): {"count": 2, "wrbtr": 110.0, "dmbtr": 96.0}}
    bad = svc.period_end_checks("r", [{}], [_bal(P, FX, AR, held={"SP20": 500.0}, prices=PR)], _tb(TP, TFX, {"SP10": 1000.0, "SP20": 0.0}, {("M1", "SP10"): ("V", 10.0), ("M2", "SP20"): ("V", 7.5)}), {"SP01"}, False, "")
    by = defaultdict(list)
    for r in bad:
        by[r.check_name].append(r)
    assert [r.status for r in by["period_trial_balance"]] == ["FAIL", "PASS"] and "differ in the period" in by["period_trial_balance"][0].explanation
    c = by["period_cutoff"][0]
    assert c.status == "FAIL" and "the source never posted in: 2025/003" in c.explanation and c.variance == "2"
    f = by["fx_open_items"][0]
    assert f.status == "WARN" and "FAGL_FCV" in f.explanation and f.evidence["local_variance"] == 4.0 and f.variance == "0.0"
    assert [r.status for r in by["inventory_valuation_by_area"]] == ["PASS", "WARN"] and "500.0 held by materials not transferred" in by["inventory_valuation_by_area"][1].explanation
    assert [r.status for r in by["material_price_control"]] == ["FAIL", "PASS"] and "M1 S 10.0 vs V 10.0" in by["material_price_control"][0].explanation
    # a missing period and a count mismatch of the foreign-currency items
    miss = svc.period_end_checks("r", [{}], [_bal(P, FX, AR, prices=PR)], _tb({("SP01", "2025", "001"): P[("SP01", "2025", "001")]}, {("SP01", "USD"): {"count": 1, "wrbtr": 55.0, "dmbtr": 50.0}}, AR, PR), {"SP01"}, False, "")
    by = {r.check_name: r for r in miss}
    assert by["period_cutoff"].status == "WARN" and "no posting in the target: 2025/002" in by["period_cutoff"].explanation
    assert by["fx_open_items"].status == "WARN" and "2 vs 1 open item(s) in USD" in by["fx_open_items"].explanation
    # paths that cannot know say so
    na = svc.period_end_checks("r", [{}], [_bal(None, None, AR)], _tb(None, None, AR, None, mode="aggregate", price_counts={("SP10", "S"): 1, ("SP20", "V"): 1}), {"SP01"}, True, " (aggregate)")
    by = defaultdict(list)
    for r in na:
        by[r.check_name].append(r)
    assert all(by[n][0].status == "NOT_VERIFIED" and by[n][0].evidence.get("unavailable") for n in ("period_trial_balance", "period_cutoff", "fx_open_items"))
    assert "no fiscal period in aggregate mode" in by["period_trial_balance"][0].explanation and "not from BSEG in aggregate mode" in by["fx_open_items"][0].explanation
    # the headers' periods are known on every path: the cut-off verifies the target's periods even when the transferred ones are not known
    hdr = svc.period_end_checks("r", [{}], [_bal(None, None, AR, periods_all={("SP01", "2025", "001"), ("SP01", "2025", "002")})], _tb(None, None, AR, None, mode="aggregate", periods_all={("SP01", "2025", "001")}), {"SP01"}, True, "")
    c = next(r for r in hdr if r.check_name == "period_cutoff")
    assert c.status == "PASS" and "missing periods are not verified" in c.explanation and c.evidence["transferred_periods_verified"] is False
    src_counts = _bal(None, None, AR)
    src_counts.price_control, src_counts.price_counts = None, {("SP10", "S"): 1, ("SP20", "V"): 2}
    cnt = svc.period_end_checks("r", [{}], [src_counts], _tb(None, None, AR, None, mode="aggregate", price_counts={("SP10", "S"): 1, ("SP20", "V"): 1}), {"SP01"}, True, "")
    pc = [r for r in cnt if r.check_name == "material_price_control"]
    assert [r.status for r in pc] == ["PASS", "WARN"] and pc[1].source_value == "V: 2" and pc[1].target_value == "V: 1" and "compare the prices by report (CKM3)" in pc[1].explanation
    unreadable = svc.period_end_checks("r", [{}], [_bal(P, FX, AR, prices=PR)], _tb(P, FX, AR, None), {"SP01"}, False, "")
    r = next(x for x in unreadable if x.check_name == "material_price_control")
    assert r.status == "WARN" and r.evidence.get("unreadable") and "MM03 / CKM3" in r.explanation
    none = svc.period_end_checks("r", [{}], [_bal(P, {}, AR, prices=PR)], _tb(P, {}, AR, PR), {"SP01"}, False, "")
    assert next(x for x in none if x.check_name == "fx_open_items").explanation.startswith("no open items in a foreign currency")

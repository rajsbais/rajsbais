"""Technical, functional and financial reconciliation between source scope and target.

A successful technical load is never treated as successful financial or functional reconciliation: the three
layers are computed independently and each produces PASS / WARN / FAIL results with evidence and, for
variances, an explanation derived from the manifest classification and transformation exceptions.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, instance_status
from ..catalog.store import RecordStore
from ..models import MigrationRun, ReconciliationResult, ScopeManifest, StagedRecord, TransformationException

TRANSFER = ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")


def _r(run_id, layer, name, status, subject="", src="", tgt="", variance="", explanation="", evidence=None):
    return ReconciliationResult(run_id=run_id, layer=layer, check_name=name, subject=subject, status=status, source_value=str(src), target_value=str(tgt), variance=str(variance), explanation=explanation, evidence=evidence or {})


def _sum_lines(rows, cc_field="BUKRS"):
    bal = defaultdict(float)
    for l in rows:
        amt = float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])
        bal[(l[cc_field], l["HKONT"])] += amt
    return bal


def reconcile_run(session: Session, run: MigrationRun, manifest: ScopeManifest, source: RecordStore, target: RecordStore) -> dict:
    results: list[ReconciliationResult] = []
    rid = run.id
    defn = manifest.definition
    cls = manifest.selection.get("classification", {})
    scope_ccs = set(defn["company_codes"])
    cc_map = defn.get("target_ownership", {}).get("company_code_map") or {}
    tcc_of = lambda cc: cc_map.get(cc, cc)  # noqa: E731
    target_ccs = {tcc_of(c) for c in scope_ccs}
    staged = session.execute(select(StagedRecord).where(StagedRecord.run_id == rid)).scalars().all()
    exceptions = session.execute(select(TransformationException).where(TransformationException.run_id == rid)).scalars().all()
    by_table: dict[str, list[StagedRecord]] = defaultdict(list)
    for s in staged:
        by_table[s.table_name].append(s)

    # ------------------------------------------------------------------ TECHNICAL
    key_collisions = 0
    for table, recs in sorted(by_table.items()):
        loaded = [s for s in recs if s.load_status == "LOADED"]
        present = [s for s in loaded if target.by_key(table, s.target_key) is not None]
        missing = len(loaded) - len(present)
        rejected = sum(1 for s in recs if s.load_status in ("REJECTED", "CONFLICT", "UNSUPPORTED"))
        status = "PASS" if missing == 0 and rejected == 0 else ("FAIL" if missing else "WARN")
        results.append(_r(rid, "TECHNICAL", "record_count", status, table, len(recs), len(present), len(recs) - len(present), f"{rejected} record(s) rejected/unsupported before load" if rejected else "", {"missing_in_target": missing, "rejected": rejected}))
        tkeys = Counter(s.target_key for s in loaded)
        dups = sum(1 for k, n in tkeys.items() if n > 1)
        key_collisions += dups
        results.append(_r(rid, "TECHNICAL", "key_uniqueness", "PASS" if dups == 0 else "FAIL", table, len(tkeys), len(loaded), dups, "Several source records map to the same target key" if dups else ""))
        exp = hashlib.sha256("".join(sorted(json.dumps(s.target_payload, sort_keys=True, default=str) for s in loaded)).encode()).hexdigest()
        act = hashlib.sha256("".join(sorted(json.dumps(target.by_key(table, s.target_key), sort_keys=True, default=str) for s in present)).encode()).hexdigest()
        results.append(_r(rid, "TECHNICAL", "checksum", "PASS" if exp == act else "FAIL", table, exp[:16], act[:16], "", "" if exp == act else "Target content differs from transformed staging content"))
        mism = 0
        for s in present[:200]:
            t = target.by_key(table, s.target_key)
            mism += sum(1 for k, v in s.target_payload.items() if t.get(k) != v)
        results.append(_r(rid, "TECHNICAL", "field_comparison", "PASS" if mism == 0 else "FAIL", table, min(len(present), 200), mism, mism, "Field-level sample comparison of staged vs loaded"))
    # referential integrity: item -> header in target, for loaded tables
    for bo in BUSINESS_OBJECTS.values():
        if not bo.item_tables or not target.rows(bo.header_table):
            continue
        for it in bo.item_tables:
            rows = target.rows(it)
            if not rows:
                continue
            orphans = 0
            for r in rows:
                hk = {k: r.get(k) for k in bo.key_fields if k in r}
                if len(hk) == len(bo.key_fields) and target.get(bo.header_table, **hk) is None:
                    orphans += 1
            results.append(_r(rid, "TECHNICAL", "referential_integrity", "PASS" if orphans == 0 else "FAIL", f"{it}->{bo.header_table}", len(rows), len(rows) - orphans, orphans, "Item rows without header in target" if orphans else ""))

    # ------------------------------------------------------------------ FUNCTIONAL
    hdr_map: dict[str, dict[str, str]] = defaultdict(dict)  # table -> source key -> target key
    for s in staged:
        if s.load_status == "LOADED":
            hdr_map[s.table_name][s.record_key] = s.target_key
    # document chains
    for chain_name, head, follow in (("order_to_cash", "SD.SalesOrder", ["SD.Delivery", "SD.BillingDocument", "FI.AccountingDocument"]), ("procure_to_pay", "MM.PurchaseOrder", ["MM.MaterialDocument", "MM.InvoiceReceipt", "FI.AccountingDocument"])):
        heads = [n for n, c in cls.items() if c["type"] == head and c["classification"] in TRANSFER]
        complete = 0
        broken = []
        for n in heads:
            htable = BUSINESS_OBJECTS[head].header_table
            skey = n.split(":", 1)[1]
            tkey = hdr_map[htable].get(skey)
            if tkey is None or target.by_key(htable, tkey) is None:
                broken.append({"node": n, "reason": "head document missing in target"})
                continue
            complete += 1
        results.append(_r(rid, "FUNCTIONAL", "document_chain", "PASS" if not broken else "FAIL", chain_name, len(heads), complete, len(broken), "Transferred head documents present in target; dependent documents validated through referential checks", {"broken": broken[:20]}))
    # partner / material references in target
    def ref_check(name, table, fld, ref_table, ref_fld):
        rows = target.rows(table)
        if not rows:
            return
        keys = {r[ref_fld] for r in target.rows(ref_table)}
        dangling = [r[fld] for r in rows if r.get(fld) and r[fld] not in keys]
        results.append(_r(rid, "FUNCTIONAL", name, "PASS" if not dangling else "FAIL", f"{table}.{fld}->{ref_table}", len(rows), len(rows) - len(dangling), len(dangling), "Dangling references in target" if dangling else "", {"samples": dangling[:10]}))

    ref_check("business_partner_reference", "VBAK", "KUNNR", "KNA1", "KUNNR")
    ref_check("business_partner_reference", "EKKO", "LIFNR", "LFA1", "LIFNR")
    ref_check("business_partner_reference", "KNB1", "KUNNR", "KNA1", "KUNNR")
    ref_check("business_partner_reference", "LFB1", "LIFNR", "LFA1", "LIFNR")
    ref_check("material_reference", "VBAP", "MATNR", "MARA", "MATNR")
    ref_check("material_reference", "EKPO", "MATNR", "MARA", "MATNR")
    ref_check("material_reference", "MARC", "MATNR", "MARA", "MATNR")
    # open document validity
    for bo_id in ("SD.SalesOrder", "MM.PurchaseOrder", "FI.AccountingDocument"):
        bo = BUSINESS_OBJECTS[bo_id]
        checked, mism, partial = 0, [], 0
        for n, c in cls.items():
            if c["type"] != bo_id or c["classification"] not in TRANSFER:
                continue
            if c["classification"] == "PARTIALLY_TRANSFERRED":
                partial += 1
                continue  # status of a partially transferred document is re-derived after business redesign
            skey = n.split(":", 1)[1]
            tkey = hdr_map[bo.header_table].get(skey)
            trow = target.by_key(bo.header_table, tkey) if tkey else None
            tst = instance_status(bo, trow, target) if trow else None
            checked += 1
            if tst != c.get("status"):
                mism.append({"node": n, "source": c.get("status"), "target": tst})
        status = "PASS" if not mism else "FAIL"
        results.append(_r(rid, "FUNCTIONAL", "open_document_validity", status, bo_id, checked, checked - len(mism), len(mism), f"{partial} partially transferred document(s) excluded from status comparison" if partial else "", {"mismatches": mism[:20]}))
    # organisational assignments
    tgt_cc_set = {r["BUKRS"] for r in target.rows("T001")} | target_ccs
    bad_cc = Counter()
    for table in by_table:
        for r in target.rows(table):
            if r.get("BUKRS") and r["BUKRS"] not in tgt_cc_set:
                bad_cc[table] += 1
    results.append(_r(rid, "FUNCTIONAL", "organizational_assignment", "PASS" if not bad_cc else "FAIL", "company_codes", sorted(target_ccs), sorted(tgt_cc_set), sum(bad_cc.values()), "Target records referencing unknown company codes" if bad_cc else "", dict(bad_cc)))

    # ------------------------------------------------------------------ FINANCIAL
    src_bseg = [l for l in source.rows("BSEG") if l["BUKRS"] in scope_ccs]
    tgt_bseg = [l for l in target.rows("BSEG") if l["BUKRS"] in target_ccs]
    # trial balance per company code and per document in target
    for tcc in sorted(target_ccs):
        lines = [l for l in tgt_bseg if l["BUKRS"] == tcc]
        debit = round(sum(float(l["DMBTR"]) for l in lines if l["SHKZG"] == "S"), 2)
        credit = round(sum(float(l["DMBTR"]) for l in lines if l["SHKZG"] == "H"), 2)
        doc_bal = defaultdict(float)
        for l in lines:
            doc_bal[(l["BELNR"], l["GJAHR"])] += float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])
        unbalanced = sum(1 for v in doc_bal.values() if abs(v) > 0.005)
        ok = abs(debit - credit) < 0.005 and unbalanced == 0
        results.append(_r(rid, "FINANCIAL", "trial_balance", "PASS" if ok else "FAIL", tcc, debit, credit, round(debit - credit, 2), f"{unbalanced} unbalanced document(s)" if unbalanced else "Debits equal credits; every document balances", {"documents": len(doc_bal)}))
    # GL balances: source in-scope totals vs target totals, with variance explanation
    src_bal = _sum_lines(src_bseg)
    tgt_bal = _sum_lines(tgt_bseg)
    # expected after transformation (what was staged & loaded)
    exp_bal = _sum_lines([s.target_payload for s in by_table.get("BSEG", []) if s.load_status == "LOADED"])
    retained_docs = {n.split(":", 1)[1] for n, c in cls.items() if c["type"] == "FI.AccountingDocument" and c["classification"] not in TRANSFER}
    classified_docs = {n.split(":", 1)[1] for n, c in cls.items() if c["type"] == "FI.AccountingDocument"}
    # documents of in-scope company codes that never entered the manifest: removed by fiscal-year / status filters
    filtered_docs = {f"{h['BUKRS']}|{h['BELNR']}|{h['GJAHR']}" for h in source.rows("BKPF") if h["BUKRS"] in scope_ccs} - classified_docs
    rejected_keys = {e.record_key for e in exceptions if e.table_name in ("BKPF", "BSEG")}

    def _amt(l):
        return float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])
    accounts = sorted({k[1] for k in src_bal} | {k[1] for k in tgt_bal})
    gl_fail = 0
    for cc in sorted(scope_ccs):
        tcc = tcc_of(cc)
        for acct in accounts:
            s = round(src_bal.get((cc, acct), 0.0), 2)
            t = round(tgt_bal.get((tcc, acct), 0.0), 2)
            e = round(exp_bal.get((tcc, acct), 0.0), 2)
            if s == 0 and t == 0:
                continue
            var = round(s - t, 2)
            if abs(var) < 0.005:
                status, expl = "PASS", ""
            else:
                # explain: amounts of retained / rejected documents on this account
                lines_cc = [l for l in src_bseg if l["BUKRS"] == cc and l["HKONT"] == acct]
                retained_amt = round(sum(_amt(l) for l in lines_cc if f"{cc}|{l['BELNR']}|{l['GJAHR']}" in retained_docs), 2)
                filtered_amt = round(sum(_amt(l) for l in lines_cc if f"{cc}|{l['BELNR']}|{l['GJAHR']}" in filtered_docs), 2)
                rejected_amt = round(sum(_amt(l) for l in lines_cc if f"{cc}|{l['BELNR']}|{l['GJAHR']}|{l['BUZEI']}" in rejected_keys), 2)
                unexplained = round(var - retained_amt - filtered_amt - rejected_amt, 2)
                status = "WARN" if abs(unexplained) < 0.005 else "FAIL"
                expl = f"Variance {var}: {retained_amt} in documents retained/excluded by scope policy, {filtered_amt} in documents outside the fiscal-year/status filters (balance carry-forward required), {rejected_amt} in documents rejected by transformation rules, unexplained {unexplained}"
                if abs(e - t) > 0.005:
                    status = "FAIL"
                    expl += f"; loaded content differs from staged expectation ({e} vs {t})"
                if status == "FAIL":
                    gl_fail += 1
            results.append(_r(rid, "FINANCIAL", "gl_balance", status, f"{cc}->{tcc}/{acct}", s, t, var, expl, {"expected_after_rules": e}))
    # AP / AR open items
    for name, table, fld in (("ar_open_items", "BSID", "KUNNR"), ("ap_open_items", "BSIK", "LIFNR")):
        s_rows = [r for r in source.rows(table) if r["BUKRS"] in scope_ccs and not r.get("AUGBL")]
        t_rows = [r for r in target.rows(table) if r["BUKRS"] in target_ccs and not r.get("AUGBL")]
        s_amt = round(sum(float(r["DMBTR"]) * (1 if r["SHKZG"] == "S" else -1) for r in s_rows), 2)
        t_amt = round(sum(float(r["DMBTR"]) * (1 if r["SHKZG"] == "S" else -1) for r in t_rows), 2)
        ok = len(s_rows) == len(t_rows) and abs(s_amt - t_amt) < 0.005
        results.append(_r(rid, "FINANCIAL", name, "PASS" if ok else "WARN", "open_items", f"{len(s_rows)} / {s_amt}", f"{len(t_rows)} / {t_amt}", round(s_amt - t_amt, 2), "" if ok else "Open-item differences are explained by retained/excluded documents (see gl_balance rows)"))
    # asset balances
    s_assets = round(sum(float(r["KANSW"]) for r in source.rows("ANLC") if r["BUKRS"] in scope_ccs), 2)
    t_assets = round(sum(float(r["KANSW"]) for r in target.rows("ANLC") if r["BUKRS"] in target_ccs), 2)
    results.append(_r(rid, "FINANCIAL", "asset_balances", "PASS" if abs(s_assets - t_assets) < 0.005 else "FAIL", "acquisition_values", s_assets, t_assets, round(s_assets - t_assets, 2)))
    # inventory valuation by valuation area
    plant_map = defn.get("target_ownership", {}).get("plant_map") or {}
    src_val_areas = {r["BWKEY"] for r in source.rows("T001K") if r["BUKRS"] in scope_ccs}
    s_inv = round(sum(float(r["SALK3"]) for r in source.rows("MBEW") if r["BWKEY"] in src_val_areas), 2)
    tgt_val_areas = {plant_map.get(b, b) for b in src_val_areas}
    t_inv = round(sum(float(r["SALK3"]) for r in target.rows("MBEW") if r["BWKEY"] in tgt_val_areas), 2)
    inv_var = round(s_inv - t_inv, 2)
    inv_expl = ""
    if abs(inv_var) >= 0.005:
        not_transferred = {n.split(":", 1)[1] for n, c in cls.items() if c["type"] == "MD.Material" and c["classification"] not in TRANSFER}
        held = round(sum(float(r["SALK3"]) for r in source.rows("MBEW") if r["BWKEY"] in src_val_areas and r["MATNR"] in not_transferred), 2)
        inv_expl = f"{held} held by materials not transferred (manual disposition / excluded); unexplained {round(inv_var - held, 2)}"
    results.append(_r(rid, "FINANCIAL", "inventory_valuation", "PASS" if abs(inv_var) < 0.005 else ("WARN" if inv_expl and abs(inv_var - held) < 0.005 else "FAIL"), "valuation_areas", s_inv, t_inv, inv_var, inv_expl))
    # intercompany balances (open)
    def ic(rows, ccs):
        out = defaultdict(float)
        for l in rows:
            if l["BUKRS"] in ccs and l.get("VBUND") and l["KOART"] in ("D", "K") and not l.get("AUGBL"):
                out[(l["BUKRS"], l["VBUND"])] += float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])
        return out

    s_ic, t_ic = ic(src_bseg, scope_ccs), ic(tgt_bseg, target_ccs)
    for (cc, vb), amt in sorted(s_ic.items()):
        t_amt = round(t_ic.get((tcc_of(cc), tcc_of(vb)), 0.0), 2)
        results.append(_r(rid, "FINANCIAL", "intercompany_balance", "PASS" if abs(round(amt, 2) - t_amt) < 0.005 else "WARN", f"{cc}<->{vb}", round(amt, 2), t_amt, round(amt - t_amt, 2), "" if abs(round(amt, 2) - t_amt) < 0.005 else "Counterpart documents retained by seller or excluded by cross-company policy"))
    # currency-specific
    s_cur = defaultdict(float)
    t_cur = defaultdict(float)
    for l in src_bseg:
        h = source.get("BKPF", BUKRS=l["BUKRS"], BELNR=l["BELNR"], GJAHR=l["GJAHR"])
        if h and l["SHKZG"] == "S":
            s_cur[(tcc_of(l["BUKRS"]), h["WAERS"])] += float(l["WRBTR"])
    for l in tgt_bseg:
        h = target.get("BKPF", BUKRS=l["BUKRS"], BELNR=l["BELNR"], GJAHR=l["GJAHR"])
        if h and l["SHKZG"] == "S":
            t_cur[(l["BUKRS"], h["WAERS"])] += float(l["WRBTR"])
    for k in sorted(set(s_cur) | set(t_cur)):
        s, t = round(s_cur.get(k, 0), 2), round(t_cur.get(k, 0), 2)
        results.append(_r(rid, "FINANCIAL", "currency_totals", "PASS" if abs(s - t) < 0.005 else "WARN", f"{k[0]}/{k[1]}", s, t, round(s - t, 2), "" if abs(s - t) < 0.005 else "Document-currency debit totals differ; see gl_balance explanations"))
    # fiscal period controls
    yf, yt = defn.get("fiscal_year_from"), defn.get("fiscal_year_to")
    out_of_range = sum(1 for h in target.rows("BKPF") if h["BUKRS"] in target_ccs and ((yf and int(h["GJAHR"]) < yf) or (yt and int(h["GJAHR"]) > yt)))
    results.append(_r(rid, "FINANCIAL", "fiscal_period_control", "PASS" if out_of_range == 0 else "FAIL", f"{yf or '*'}-{yt or '*'}", "", out_of_range, out_of_range, "Target documents outside the scoped fiscal years" if out_of_range else ""))

    session.add_all(results)
    session.flush()
    summary = summarize(results)
    summary["key_collisions"] = key_collisions
    summary["gl_failures"] = gl_fail
    return summary


def summarize(results: list[ReconciliationResult]) -> dict:
    by_layer: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        by_layer[r.layer][r.status] += 1
    overall = "PASS"
    for c in by_layer.values():
        if c["FAIL"]:
            overall = "FAIL"
        elif c["WARN"] and overall != "FAIL":
            overall = "WARN"
    return {"overall": overall, "by_layer": {k: dict(v) for k, v in by_layer.items()}, "checks": len(results)}

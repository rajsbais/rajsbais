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

from .. import observability as obs
from ..catalog.business_objects import BUSINESS_OBJECTS, instance_status
from ..catalog.store import RecordStore
from ..models import MigrationRun, ReconciliationResult, ScopeManifest, TransformationException
from ..staging import get_backend

TRANSFER = ("FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED")


def _r(run_id, layer, name, status, subject="", src="", tgt="", variance="", explanation="", evidence=None):
    return ReconciliationResult(run_id=run_id, layer=layer, check_name=name, subject=subject, status=status, source_value=str(src), target_value=str(tgt), variance=str(variance), explanation=explanation, evidence=evidence or {})


def _sum_lines(rows, cc_field="BUKRS"):
    bal = defaultdict(float)
    for l in rows:
        amt = float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])
        bal[(l[cc_field], l["HKONT"])] += amt
    return bal


def _item_tables_of(table: str):
    """(business object, header table) pairs for which `table` is an item table."""
    return [(bo, bo.header_table) for bo in BUSINESS_OBJECTS.values() if table in bo.item_tables]


def technical_checks_for_table(rid: str, table: str, recs: list, target: RecordStore) -> tuple[list[ReconciliationResult], int]:
    """Technical layer for one staged table: record counts, target-key uniqueness, checksum, field-level sample
    comparison and, for item tables, item -> header referential integrity in the target."""
    results: list[ReconciliationResult] = []
    loaded = [s for s in recs if s.load_status == "LOADED"]
    present = [s for s in loaded if target.by_key(table, s.target_key) is not None]
    missing = len(loaded) - len(present)
    rejected = sum(1 for s in recs if s.load_status in ("REJECTED", "CONFLICT", "UNSUPPORTED"))
    status = "PASS" if missing == 0 and rejected == 0 else ("FAIL" if missing else "WARN")
    results.append(_r(rid, "TECHNICAL", "record_count", status, table, len(recs), len(present), len(recs) - len(present), f"{rejected} record(s) rejected/unsupported before load" if rejected else "", {"missing_in_target": missing, "rejected": rejected}))
    tkeys = Counter(s.target_key for s in {x.record_key: x for x in loaded}.values())  # identical copies staged by two partitions are one record
    dups = sum(1 for k, n in tkeys.items() if n > 1)
    results.append(_r(rid, "TECHNICAL", "key_uniqueness", "PASS" if dups == 0 else "FAIL", table, len(tkeys), len(loaded), dups, "Several source records map to the same target key" if dups else ""))
    exp = hashlib.sha256("".join(sorted(json.dumps(s.target_payload, sort_keys=True, default=str) for s in loaded)).encode()).hexdigest()
    act = hashlib.sha256("".join(sorted(json.dumps(target.by_key(table, s.target_key), sort_keys=True, default=str) for s in present)).encode()).hexdigest()
    results.append(_r(rid, "TECHNICAL", "checksum", "PASS" if exp == act else "FAIL", table, exp[:16], act[:16], "", "" if exp == act else "Target content differs from transformed staging content"))
    mism = 0
    for s in present[:200]:
        t = target.by_key(table, s.target_key)
        mism += sum(1 for k, v in s.target_payload.items() if t.get(k) != v)
    results.append(_r(rid, "TECHNICAL", "field_comparison", "PASS" if mism == 0 else "FAIL", table, min(len(present), 200), mism, mism, "Field-level sample comparison of staged vs loaded"))
    for bo, header in _item_tables_of(table):
        rows = target.rows(table)
        if not rows or not target.rows(header):
            continue
        orphans = 0
        for r in rows:
            hk = {k: r.get(k) for k in bo.key_fields if k in r}
            if len(hk) == len(bo.key_fields) and target.get(header, **hk) is None:
                orphans += 1
        results.append(_r(rid, "TECHNICAL", "referential_integrity", "PASS" if orphans == 0 else "FAIL", f"{table}->{header}", len(rows), len(rows) - orphans, orphans, "Item rows without header in target" if orphans else ""))
    return results, dups


def functional_checks(rid: str, manifest: ScopeManifest, hdr_map: dict, loaded_tables: set[str], target: RecordStore) -> list[ReconciliationResult]:
    """Functional layer: document-chain completeness, partner/material references, open-document validity and
    organisational assignments. hdr_map: table -> source key -> target key of loaded records."""
    results: list[ReconciliationResult] = []
    defn = manifest.definition
    cls = manifest.selection.get("classification", {})
    cc_map = defn.get("target_ownership", {}).get("company_code_map") or {}
    target_ccs = {cc_map.get(c, c) for c in defn["company_codes"]}
    for chain_name, head in (("order_to_cash", "SD.SalesOrder"), ("procure_to_pay", "MM.PurchaseOrder")):
        heads = [n for n, c in cls.items() if c["type"] == head and c["classification"] in TRANSFER]
        complete = 0
        broken = []
        htable = BUSINESS_OBJECTS[head].header_table
        for n in heads:
            skey = n.split(":", 1)[1]
            tkey = hdr_map.get(htable, {}).get(skey)
            if tkey is None or target.by_key(htable, tkey) is None:
                broken.append({"node": n, "reason": "head document missing in target"})
                continue
            complete += 1
        results.append(_r(rid, "FUNCTIONAL", "document_chain", "PASS" if not broken else "FAIL", chain_name, len(heads), complete, len(broken), "Transferred head documents present in target; dependent documents validated through referential checks", {"broken": broken[:20]}))

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
            tkey = hdr_map.get(bo.header_table, {}).get(skey)
            trow = target.by_key(bo.header_table, tkey) if tkey else None
            tst = instance_status(bo, trow, target) if trow else None
            checked += 1
            if tst != c.get("status"):
                mism.append({"node": n, "source": c.get("status"), "target": tst})
        status = "PASS" if not mism else "FAIL"
        results.append(_r(rid, "FUNCTIONAL", "open_document_validity", status, bo_id, checked, checked - len(mism), len(mism), f"{partial} partially transferred document(s) excluded from status comparison" if partial else "", {"mismatches": mism[:20]}))
    tgt_cc_set = {r["BUKRS"] for r in target.rows("T001")} | target_ccs
    bad_cc = Counter()
    for table in loaded_tables:
        for r in target.rows(table):
            if r.get("BUKRS") and r["BUKRS"] not in tgt_cc_set:
                bad_cc[table] += 1
    results.append(_r(rid, "FUNCTIONAL", "organizational_assignment", "PASS" if not bad_cc else "FAIL", "company_codes", sorted(target_ccs), sorted(tgt_cc_set), sum(bad_cc.values()), "Target records referencing unknown company codes" if bad_cc else "", dict(bad_cc)))
    return results


def _hdr_map(staged) -> tuple[dict, set[str]]:
    hdr_map: dict[str, dict[str, str]] = defaultdict(dict)
    tables = set()
    for s in staged:
        tables.add(s.table_name)
        if s.load_status == "LOADED":
            hdr_map[s.table_name][s.record_key] = s.target_key
    return hdr_map, tables


def reconcile_run(session: Session, run: MigrationRun, manifest: ScopeManifest, source: RecordStore, target: RecordStore, financial: bool = True, backend=None) -> dict:
    """Inline reconciliation: all three layers in one call. Distributed runs execute the same functions as
    RECONCILE jobs (see reconcile_partition)."""
    results: list[ReconciliationResult] = []
    rid = run.id
    cls = manifest.selection.get("classification", {})
    backend = backend or get_backend(run.metrics.get("staging_backend"), session=session)
    staged = list(backend.iter_records(rid))
    exceptions = session.execute(select(TransformationException).where(TransformationException.run_id == rid)).scalars().all()
    by_table: dict[str, list] = defaultdict(list)
    for s in staged:
        by_table[s.table_name].append(s)
    key_collisions = 0
    for table, recs in sorted(by_table.items()):
        res, dups = technical_checks_for_table(rid, table, recs, target)
        results.extend(res)
        key_collisions += dups
    hdr_map, tables = _hdr_map(staged)
    results.extend(functional_checks(rid, manifest, hdr_map, tables, target))
    gl_fail = 0
    if financial:
        ctx = source_context(manifest, source, cls, exceptions, [s.target_payload for s in by_table.get("BSEG", []) if s.load_status == "LOADED"])
        fin, gl_fail = financial_checks(rid, [ctx], target)
        results.extend(fin)

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
    for layer, c in by_layer.items():
        for st, n in c.items():
            obs.counter("sdtf.reconciliation.checks", n, layer=layer, status=st)
    overall = "PASS"
    for c in by_layer.values():
        if c["FAIL"]:
            overall = "FAIL"
        elif c["WARN"] and overall != "FAIL":
            overall = "WARN"
    return {"overall": overall, "by_layer": {k: dict(v) for k, v in by_layer.items()}, "checks": len(results)}


# ====================================================================== FINANCIAL (single- or multi-source)
def source_context(manifest: ScopeManifest, store: RecordStore, cls: dict, exceptions: list, loaded_bseg_payloads: list[dict]) -> dict:
    defn = manifest.definition
    cc_map = defn.get("target_ownership", {}).get("company_code_map") or {}
    scope_ccs = set(defn["company_codes"])
    return {"store": store, "defn": defn, "cls": cls, "scope_ccs": scope_ccs, "tcc_of": (lambda cc: cc_map.get(cc, cc)), "plant_map": defn.get("target_ownership", {}).get("plant_map") or {}, "rejected_keys": {e.record_key for e in exceptions if e.table_name in ("BKPF", "BSEG")}, "loaded_bseg": loaded_bseg_payloads}


def _amt(l):
    return float(l["DMBTR"]) if l["SHKZG"] == "S" else -float(l["DMBTR"])


def financial_checks(rid: str, sources: list[dict], target: RecordStore) -> tuple[list[ReconciliationResult], int]:
    """Financial reconciliation of one or several sources (merge group) against one target. Source totals are
    mapped to target company codes and aggregated before comparison; variances are explained per bucket."""
    results: list[ReconciliationResult] = []
    target_ccs = {c["tcc_of"](cc) for c in sources for cc in c["scope_ccs"]}
    src_label: dict[str, list[str]] = defaultdict(list)
    for c in sources:
        for cc in sorted(c["scope_ccs"]):
            src_label[c["tcc_of"](cc)].append(cc)
    tgt_bseg = [l for l in target.rows("BSEG") if l["BUKRS"] in target_ccs]
    # trial balance per target company code and per document
    for tcc in sorted(target_ccs):
        lines = [l for l in tgt_bseg if l["BUKRS"] == tcc]
        debit = round(sum(float(l["DMBTR"]) for l in lines if l["SHKZG"] == "S"), 2)
        credit = round(sum(float(l["DMBTR"]) for l in lines if l["SHKZG"] == "H"), 2)
        doc_bal = defaultdict(float)
        for l in lines:
            doc_bal[(l["BELNR"], l["GJAHR"])] += _amt(l)
        unbalanced = sum(1 for v in doc_bal.values() if abs(v) > 0.005)
        ok = abs(debit - credit) < 0.005 and unbalanced == 0
        results.append(_r(rid, "FINANCIAL", "trial_balance", "PASS" if ok else "FAIL", tcc, debit, credit, round(debit - credit, 2), f"{unbalanced} unbalanced document(s)" if unbalanced else "Debits equal credits; every document balances", {"documents": len(doc_bal)}))
    # GL balances aggregated by target company code
    src_bal: dict[tuple, float] = defaultdict(float)
    retained_b: dict[tuple, float] = defaultdict(float)
    filtered_b: dict[tuple, float] = defaultdict(float)
    rejected_b: dict[tuple, float] = defaultdict(float)
    exp_bal: dict[tuple, float] = defaultdict(float)
    for c in sources:
        store, cls, scope_ccs, tcc_of = c["store"], c["cls"], c["scope_ccs"], c["tcc_of"]
        retained = {n.split(":", 1)[1] for n, x in cls.items() if x["type"] == "FI.AccountingDocument" and x["classification"] not in TRANSFER}
        classified = {n.split(":", 1)[1] for n, x in cls.items() if x["type"] == "FI.AccountingDocument"}
        filtered = {f"{h['BUKRS']}|{h['BELNR']}|{h['GJAHR']}" for h in store.rows("BKPF") if h["BUKRS"] in scope_ccs} - classified
        for l in store.rows("BSEG"):
            if l["BUKRS"] not in scope_ccs:
                continue
            k = (tcc_of(l["BUKRS"]), l["HKONT"])
            a = _amt(l)
            src_bal[k] += a
            doc = f"{l['BUKRS']}|{l['BELNR']}|{l['GJAHR']}"
            if doc in retained:
                retained_b[k] += a
            elif doc in filtered:
                filtered_b[k] += a
            elif f"{doc}|{l['BUZEI']}" in c["rejected_keys"]:
                rejected_b[k] += a
        for k, v in _sum_lines(c["loaded_bseg"]).items():
            exp_bal[k] += v
    tgt_bal = _sum_lines(tgt_bseg)
    gl_fail = 0
    for k in sorted(set(src_bal) | set(tgt_bal)):
        tcc, acct = k
        if tcc not in target_ccs:
            continue
        s, t, e = round(src_bal.get(k, 0.0), 2), round(tgt_bal.get(k, 0.0), 2), round(exp_bal.get(k, 0.0), 2)
        if s == 0 and t == 0:
            continue
        var = round(s - t, 2)
        if abs(var) < 0.005:
            status, expl = "PASS", ""
        else:
            ra, fa, ja = round(retained_b.get(k, 0.0), 2), round(filtered_b.get(k, 0.0), 2), round(rejected_b.get(k, 0.0), 2)
            unexplained = round(var - ra - fa - ja, 2)
            status = "WARN" if abs(unexplained) < 0.005 else "FAIL"
            expl = f"Variance {var}: {ra} in documents retained/excluded by scope policy, {fa} in documents outside the fiscal-year/status filters (balance carry-forward required), {ja} in documents rejected by transformation rules, unexplained {unexplained}"
            if abs(e - t) > 0.005:
                status = "FAIL"
                expl += f"; loaded content differs from staged expectation ({e} vs {t})"
            if status == "FAIL":
                gl_fail += 1
        results.append(_r(rid, "FINANCIAL", "gl_balance", status, f"{'+'.join(src_label[tcc])}->{tcc}/{acct}", s, t, var, expl, {"expected_after_rules": e}))
    # AP / AR open items
    for name, table in (("ar_open_items", "BSID"), ("ap_open_items", "BSIK")):
        s_rows = [r for c in sources for r in c["store"].rows(table) if r["BUKRS"] in c["scope_ccs"] and not r.get("AUGBL")]
        t_rows = [r for r in target.rows(table) if r["BUKRS"] in target_ccs and not r.get("AUGBL")]
        s_amt = round(sum(_amt(r) for r in s_rows), 2)
        t_amt = round(sum(_amt(r) for r in t_rows), 2)
        ok = len(s_rows) == len(t_rows) and abs(s_amt - t_amt) < 0.005
        results.append(_r(rid, "FINANCIAL", name, "PASS" if ok else "WARN", "open_items", f"{len(s_rows)} / {s_amt}", f"{len(t_rows)} / {t_amt}", round(s_amt - t_amt, 2), "" if ok else "Open-item differences are explained by retained/excluded documents (see gl_balance rows)"))
    # asset balances
    s_assets = round(sum(float(r["KANSW"]) for c in sources for r in c["store"].rows("ANLC") if r["BUKRS"] in c["scope_ccs"]), 2)
    t_assets = round(sum(float(r["KANSW"]) for r in target.rows("ANLC") if r["BUKRS"] in target_ccs), 2)
    results.append(_r(rid, "FINANCIAL", "asset_balances", "PASS" if abs(s_assets - t_assets) < 0.005 else "FAIL", "acquisition_values", s_assets, t_assets, round(s_assets - t_assets, 2)))
    # inventory valuation by valuation area
    s_inv, held, tgt_val_areas = 0.0, 0.0, set()
    for c in sources:
        store = c["store"]
        areas = {r["BWKEY"] for r in store.rows("T001K") if r["BUKRS"] in c["scope_ccs"]}
        tgt_val_areas |= {c["plant_map"].get(b, b) for b in areas}
        not_transferred = {n.split(":", 1)[1] for n, x in c["cls"].items() if x["type"] == "MD.Material" and x["classification"] not in TRANSFER}
        for r in store.rows("MBEW"):
            if r["BWKEY"] in areas:
                s_inv += float(r["SALK3"])
                if r["MATNR"] in not_transferred:
                    held += float(r["SALK3"])
    s_inv, held = round(s_inv, 2), round(held, 2)
    t_inv = round(sum(float(r["SALK3"]) for r in target.rows("MBEW") if r["BWKEY"] in tgt_val_areas), 2)
    inv_var = round(s_inv - t_inv, 2)
    inv_expl = "" if abs(inv_var) < 0.005 else f"{held} held by materials not transferred (manual disposition / excluded); unexplained {round(inv_var - held, 2)}"
    results.append(_r(rid, "FINANCIAL", "inventory_valuation", "PASS" if abs(inv_var) < 0.005 else ("WARN" if abs(inv_var - held) < 0.005 else "FAIL"), "valuation_areas", s_inv, t_inv, inv_var, inv_expl))
    # intercompany balances (open), aggregated on target company codes
    s_ic: dict[tuple, float] = defaultdict(float)
    for c in sources:
        for l in c["store"].rows("BSEG"):
            if l["BUKRS"] in c["scope_ccs"] and l.get("VBUND") and l["KOART"] in ("D", "K") and not l.get("AUGBL"):
                s_ic[(c["tcc_of"](l["BUKRS"]), c["tcc_of"](l["VBUND"]))] += _amt(l)
    t_ic: dict[tuple, float] = defaultdict(float)
    for l in tgt_bseg:
        if l.get("VBUND") and l["KOART"] in ("D", "K") and not l.get("AUGBL"):
            t_ic[(l["BUKRS"], l["VBUND"])] += _amt(l)
    for k, amt in sorted(s_ic.items()):
        t_amt = round(t_ic.get(k, 0.0), 2)
        ok = abs(round(amt, 2) - t_amt) < 0.005
        results.append(_r(rid, "FINANCIAL", "intercompany_balance", "PASS" if ok else "WARN", f"{k[0]}<->{k[1]}", round(amt, 2), t_amt, round(amt - t_amt, 2), "" if ok else "Counterpart documents retained by seller or excluded by cross-company policy"))
    # currency-specific debit totals
    s_cur: dict[tuple, float] = defaultdict(float)
    t_cur: dict[tuple, float] = defaultdict(float)
    for c in sources:
        store = c["store"]
        for l in store.rows("BSEG"):
            if l["BUKRS"] in c["scope_ccs"] and l["SHKZG"] == "S":
                h = store.get("BKPF", BUKRS=l["BUKRS"], BELNR=l["BELNR"], GJAHR=l["GJAHR"])
                if h:
                    s_cur[(c["tcc_of"](l["BUKRS"]), h["WAERS"])] += float(l["WRBTR"])
    for l in tgt_bseg:
        if l["SHKZG"] == "S":
            h = target.get("BKPF", BUKRS=l["BUKRS"], BELNR=l["BELNR"], GJAHR=l["GJAHR"])
            if h:
                t_cur[(l["BUKRS"], h["WAERS"])] += float(l["WRBTR"])
    for k in sorted(set(s_cur) | set(t_cur)):
        s, t = round(s_cur.get(k, 0), 2), round(t_cur.get(k, 0), 2)
        results.append(_r(rid, "FINANCIAL", "currency_totals", "PASS" if abs(s - t) < 0.005 else "WARN", f"{k[0]}/{k[1]}", s, t, round(s - t, 2), "" if abs(s - t) < 0.005 else "Document-currency debit totals differ; see gl_balance explanations"))
    # fiscal period controls (per source range, on that source's target company codes)
    for c in sources:
        yf, yt = c["defn"].get("fiscal_year_from"), c["defn"].get("fiscal_year_to")
        tccs = {c["tcc_of"](cc) for cc in c["scope_ccs"]}
        out_of_range = sum(1 for h in target.rows("BKPF") if h["BUKRS"] in tccs and ((yf and int(h["GJAHR"]) < yf) or (yt and int(h["GJAHR"]) > yt)))
        results.append(_r(rid, "FINANCIAL", "fiscal_period_control", "PASS" if out_of_range == 0 else "FAIL", f"{'+'.join(sorted(tccs))}:{yf or '*'}-{yt or '*'}", "", out_of_range, out_of_range, "Target documents outside the scoped fiscal years" if out_of_range else ""))
    return results, gl_fail


def reconcile_merge_group(session: Session, runs: list[MigrationRun], target: RecordStore) -> dict:
    """Group-level financial reconciliation for a multi-source merge: the union of all sources' in-scope
    balances versus the shared target. Results are attributed to the last run of the group."""
    contexts = []
    for run in runs:
        m = session.get(ScopeManifest, run.manifest_id)
        store = RecordStore.load(session, run.source_system_id)
        exceptions = session.execute(select(TransformationException).where(TransformationException.run_id == run.id)).scalars().all()
        backend = get_backend(run.metrics.get("staging_backend"), session=session)
        loaded = [s.target_payload for s in backend.iter_records(run.id, table="BSEG", status="LOADED")]
        contexts.append(source_context(m, store, m.selection.get("classification", {}), exceptions, loaded))
    last = runs[-1]
    session.query(ReconciliationResult).filter(ReconciliationResult.run_id == last.id, ReconciliationResult.layer == "FINANCIAL").delete()
    results, gl_fail = financial_checks(last.id, contexts, target)
    session.add_all(results)
    session.flush()
    summary = summarize(results)
    summary["gl_failures"] = gl_fail
    summary["sources"] = len(contexts)
    return summary


# ====================================================================== distributed RECONCILE jobs
def reconcile_partitions(session: Session, run: MigrationRun, backend=None) -> list[tuple[str, str, int]]:
    """Job list for a distributed run: one technical job per staged table, one functional, one financial
    (skipped for merge-group members). Returns (partition_id, object_type, est_rows)."""
    backend = backend or get_backend(run.metrics.get("staging_backend"), session=session)
    tables: dict[str, int] = defaultdict(int)
    for c in backend.counts(run.id):
        tables[c["table"]] += c["count"]
    jobs = [(f"technical:{t}", "RECONCILE.TECHNICAL", n) for t, n in sorted(tables.items())]
    jobs.append(("functional", "RECONCILE.FUNCTIONAL", sum(tables.values())))
    if not run.metrics.get("merge_group"):
        jobs.append(("financial", "RECONCILE.FINANCIAL", tables.get("BSEG", 0)))
    return jobs


def reconcile_partition(session: Session, run: MigrationRun, partition: str, backend=None, target: RecordStore | None = None) -> dict:
    """Execute one RECONCILE job and persist its result rows. Idempotent: previous rows of the same job are replaced."""
    rid = run.id
    m = session.get(ScopeManifest, run.manifest_id)
    backend = backend or get_backend(run.metrics.get("staging_backend"), session=session)
    target = target or RecordStore.load(session, run.target_system_id)
    if partition.startswith("technical:"):
        table = partition.split(":", 1)[1]
        session.query(ReconciliationResult).filter(ReconciliationResult.run_id == rid, ReconciliationResult.layer == "TECHNICAL", ReconciliationResult.subject.in_([table] + [f"{table}->{h}" for _, h in _item_tables_of(table)])).delete(synchronize_session=False)
        results, dups = technical_checks_for_table(rid, table, list(backend.iter_records(rid, table=table)), target)
        extra = {"key_collisions": dups}
    elif partition == "functional":
        session.query(ReconciliationResult).filter(ReconciliationResult.run_id == rid, ReconciliationResult.layer == "FUNCTIONAL").delete(synchronize_session=False)
        hdr_map, tables = _hdr_map(backend.iter_records(rid))
        results = functional_checks(rid, m, hdr_map, tables, target)
        extra = {}
    elif partition == "financial":
        session.query(ReconciliationResult).filter(ReconciliationResult.run_id == rid, ReconciliationResult.layer == "FINANCIAL").delete(synchronize_session=False)
        source = RecordStore.load(session, run.source_system_id)
        exceptions = session.execute(select(TransformationException).where(TransformationException.run_id == rid)).scalars().all()
        ctx = source_context(m, source, m.selection.get("classification", {}), exceptions, [s.target_payload for s in backend.iter_records(rid, table="BSEG", status="LOADED")])
        results, gl_fail = financial_checks(rid, [ctx], target)
        extra = {"gl_failures": gl_fail}
    else:
        raise ValueError(f"unknown reconciliation partition {partition}")
    session.add_all(results)
    session.flush()
    by = Counter(r.status for r in results)
    return {"checks": len(results), **{k: v for k, v in by.items()}, **extra}


def run_summary(session: Session, run_id: str) -> dict:
    rows = session.execute(select(ReconciliationResult).where(ReconciliationResult.run_id == run_id)).scalars().all()
    return summarize(rows)

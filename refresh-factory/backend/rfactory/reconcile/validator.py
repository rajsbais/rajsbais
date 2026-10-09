"""Technical, business and security reconciliation of a completed run, evaluated against the TARGET state."""
from __future__ import annotations

from ..dependency.planner import Plan
from ..dependency.registry import CONFIG_TYPES, Registry
from ..masking.engine import MaskingEngine, discover_sensitive
from ..sap.adapter import SourceAdapter, TargetAdapter
from ..sap.ddic import NUMBER_RANGE_OBJECTS, TABLES
from ..selective.executor import Run, row_hash


def _chk(cat, cid, name, ok, detail="", samples=None, warn=False):
    return {"id": cid, "category": cat, "name": name, "status": "pass" if ok else ("warn" if warn else "fail"),
            "detail": detail, "samples": (samples or [])[:8]}


def _split(k: str) -> tuple[str, tuple]:
    t, *parts = k.split("/")
    return t, tuple(parts)


def reconcile(run: Run, plan: Plan, source: SourceAdapter, target: TargetAdapter, masking: MaskingEngine,
              registry: Registry, required_sensitive: list[dict], row_exclusions: dict | None = None) -> dict:
    checks: list[dict] = []
    loaded = [plan.instances[i] for i in run.loaded]

    # ---------------- technical ----------------
    per_table: dict[str, list[str]] = {}
    for k in run.expected:
        per_table.setdefault(k.split("/")[0], []).append(k)
    for t in sorted(per_table):
        present = [k for k in per_table[t] if target.get(*_split(k)) is not None]
        checks.append(_chk("technical", f"TECH-COUNT-{t}", f"Row count {t}", len(present) == len(per_table[t]),
                           f"expected {len(per_table[t])}, found {len(present)}",
                           [k for k in per_table[t] if k not in set(present)]))
    bad = []
    for k, h in run.expected.items():
        row = target.get(*_split(k))
        if row is not None and row_hash(row) != h:
            bad.append(k)
    checks.append(_chk("technical", "TECH-CHECKSUM", "Row checksums match staged (masked) content", not bad,
                       f"{len(bad)} of {len(run.expected)} rows differ", bad))
    dups = []
    for t in per_table:
        seen = set()
        for r in target.select(t):
            k = tuple(r[x] for x in TABLES[t].keys)
            if k in seen:
                dups.append(f"{t}/{k}")
            seen.add(k)
    checks.append(_chk("technical", "TECH-DUP", "No duplicate keys in target", not dups, f"{len(dups)} duplicates", dups))
    missing_refs = []
    for inst in loaded:
        for d in inst.requires:
            dt, dk = d.split(":", 1)
            if target.get(registry.types[dt].header, tuple(dk.split("/"))) is None:
                missing_refs.append(f"{inst.id} -> {d}")
        for c in inst.configs:
            ct, code = c.split(":", 1)
            if target.get(CONFIG_TYPES[ct][0], (code,)) is None and inst.id not in (row_exclusions or {}):
                missing_refs.append(f"{inst.id} -> {c}")
    checks.append(_chk("technical", "TECH-REFS", "No missing references (objects and customizing)", not missing_refs,
                       f"{len(missing_refs)} dangling", missing_refs))
    typebad = []
    for t in per_table:
        for k in per_table[t][:2000]:
            row = target.get(*_split(k))
            if row and set(row) != set(TABLES[t].fields):
                typebad.append(k)
    checks.append(_chk("technical", "TECH-STRUCT", "Row structure matches data dictionary", not typebad,
                       f"{len(typebad)} rows with unexpected field set", typebad))

    # ---------------- business ----------------
    loaded_ids = {i.id for i in loaded}
    docs = lambda ty: [i for i in loaded if i.type == ty]
    flow_bad = []
    for inst in docs("DELIVERY") + docs("BILLING"):
        for f in inst.rows.get("VBFA", []):
            pre = target.get("VBAK", (f["VBELV"],)) or target.get("LIKP", (f["VBELV"],))
            suc = target.get("LIKP", (f["VBELN"],)) or target.get("VBRK", (f["VBELN"],))
            if not pre or not suc:
                flow_bad.append(f"{f['VBELV']}->{f['VBELN']}")
    checks.append(_chk("business", "BUS-DOCFLOW", "Document flow complete in target (VBFA both ends exist)",
                       not flow_bad, f"{len(flow_bad)} broken links", flow_bad))
    chain_bad = []
    for inst in docs("DELIVERY"):
        for it in inst.rows["LIPS"]:
            if not target.get("VBAK", (it["VGBEL"],)):
                chain_bad.append(f"delivery {it['VBELN']} -> order {it['VGBEL']}")
    for inst in docs("BILLING"):
        for it in inst.rows["VBRP"]:
            if not target.get("LIKP", (it["VGBEL"],)) or not target.get("VBAK", (it["AUBEL"],)):
                chain_bad.append(f"billing {it['VBELN']} -> {it['VGBEL']}/{it['AUBEL']}")
    checks.append(_chk("business", "BUS-CHAIN", "Sales document chains intact (order → delivery → billing)",
                       not chain_bad, f"{len(chain_bad)} broken", chain_bad))
    tot_bad = []
    for ty, ht, it_t, fld in (("SALES_ORDER", "VBAK", "VBAP", "VBELN"), ("BILLING", "VBRK", "VBRP", "VBELN")):
        for inst in docs(ty):
            h = target.get(ht, (inst.key,))
            items = target.lookup(it_t, fld, inst.key)
            if h and abs(h["NETWR"] - sum(i["NETWR"] for i in items)) > 0.01:
                tot_bad.append(f"{ht} {inst.key}")
    checks.append(_chk("business", "BUS-TOTALS", "Header net value equals sum of items", not tot_bad,
                       f"{len(tot_bad)} mismatches", tot_bad))
    bal_bad, fi_bad = [], []
    for inst in docs("FI_DOCUMENT"):
        b, bel, yr = inst.key.split("/")
        segs = [s for s in target.lookup("BSEG", "BELNR", bel) if s["BUKRS"] == b and s["GJAHR"] == yr]
        deb = sum(s["DMBTR"] for s in segs if s["SHKZG"] == "S")
        cre = sum(s["DMBTR"] for s in segs if s["SHKZG"] == "H")
        if abs(deb - cre) > 0.01:
            bal_bad.append(inst.key)
        hdr = inst.rows["BKPF"][0]
        if hdr["AWTYP"] == "VBRK":
            bill = target.get("VBRK", (hdr["AWKEY"],))
            recv = [s for s in segs if s["KOART"] == "D"]
            if not bill or not recv or abs(sum(s["DMBTR"] for s in recv) - bill["NETWR"]) > 0.01:
                fi_bad.append(inst.key)
    checks.append(_chk("business", "BUS-FI-BALANCE", "Accounting documents balance (debits = credits)", not bal_bad,
                       f"{len(bal_bad)} unbalanced", bal_bad))
    checks.append(_chk("business", "BUS-FI-BILLING", "Accounting documents agree with originating billing", not fi_bad,
                       f"{len(fi_bad)} inconsistent", fi_bad))
    master_bad = []
    for inst in docs("SALES_ORDER"):
        h = inst.rows["VBAK"][0]
        if not target.get("KNA1", (h["KUNNR"],)) or not target.get("KNB1", (h["KUNNR"], h["BUKRS_VF"])):
            master_bad.append(f"order {inst.key}: customer {h['KUNNR']} not extended to {h['BUKRS_VF']}")
        for it in inst.rows["VBAP"]:
            if not target.get("MARA", (it["MATNR"],)) or not target.get("MARC", (it["MATNR"], it["WERKS"])):
                master_bad.append(f"order {inst.key}: material {it['MATNR']} not maintained in plant {it['WERKS']}")
    checks.append(_chk("business", "BUS-MASTER", "Master data valid for loaded documents (customer/company, material/plant)",
                       not master_bad, f"{len(master_bad)} issues", master_bad))
    nr_bad = []
    for table, (obj, fld) in NUMBER_RANGE_OBJECTS.items():
        ks = [int(i.rows[table][0][fld]) for i in loaded if table in i.rows and i.rows[table]]
        if ks and (target.number_level(obj) or 0) < max(ks):
            nr_bad.append(f"{obj}: level {target.number_level(obj)} < max loaded {max(ks)}")
    checks.append(_chk("business", "BUS-NUMBER-RANGE", "Target number ranges protect loaded documents", not nr_bad,
                       f"{len(nr_bad)} ranges behind loaded keys", nr_bad))
    excluded = set(run.quarantined) | set(run.skipped)
    leak = [f"{i.id} -> {d}" for i in loaded for d in i.requires if d in excluded and d not in loaded_ids
            and target.get(registry.types[d.split(':')[0]].header, tuple(d.split(':', 1)[1].split('/'))) is None]
    checks.append(_chk("business", "BUS-QUARANTINE", "No loaded object depends on a quarantined object", not leak,
                       f"{len(leak)} violations", leak))

    # S/4HANA specifics: universal journal agrees with BKPF/BSEG, every customer/vendor has a Business Partner
    if any("ACDOCA" in i.rows for i in loaded):
        ac_bad = []
        for inst in docs("FI_DOCUMENT"):
            b, bel, yr = inst.key.split("/")
            lines = [a for a in target.lookup("ACDOCA", "BELNR", bel) if a["RBUKRS"] == b and a["GJAHR"] == yr]
            segs = [x for x in target.lookup("BSEG", "BELNR", bel) if x["BUKRS"] == b and x["GJAHR"] == yr]
            if len(lines) != len(segs) or abs(sum(a["HSL"] for a in lines)) > 0.01:
                ac_bad.append(inst.key)
        checks.append(_chk("business", "BUS-ACDOCA", "Universal journal (ACDOCA) balances to zero and matches BSEG line count",
                           not ac_bad, f"{len(ac_bad)} inconsistent", ac_bad))
    if any("BUT000" in i.rows for i in loaded):
        bp_bad = [i.id for i in loaded if i.type in ("CUSTOMER", "VENDOR") and
                  target.get("BUT000", (i.key,)) is None]
        checks.append(_chk("business", "BUS-BP", "Every customer/vendor has its Business Partner (CVI link)", not bp_bad,
                           f"{len(bp_bad)} without BP", bp_bad))

    # manufacturing and inventory (evaluated against the TARGET)
    orders = docs("PRODUCTION_ORDER")
    mdocs = docs("MATERIAL_DOCUMENT")
    if orders or mdocs:
        item_t = "MATDOC" if registry.types["MATERIAL_DOCUMENT"].header == "MATDOC" else "MSEG"
        struct_bad, bom_bad, comp_bad, move_bad, route_bad, conf_bad = [], [], [], [], [], []
        for inst in orders:
            n = inst.key
            afko, afpo, resb = target.get("AFKO", (n,)), target.lookup("AFPO", "AUFNR", n), target.lookup("RESB", "AUFNR", n)
            if not target.get("AUFK", (n,)) or not afko or not afpo:
                struct_bad.append(f"order {n}: header/item rows missing in target")
                continue
            stl = afko.get("STLNR")
            bom = target.get("STKO", (stl,)) if stl else None
            if stl and not bom:
                bom_bad.append(f"order {n}: BOM {stl} missing in target")
            if bom:
                for comp in target.lookup("STPO", "STLNR", stl):
                    need = comp["MENGE"] * afko["GAMNG"] / (bom["BMENG"] or 1)
                    got = sum(r["BDMNG"] for r in resb if r["MATNR"] == comp["IDNRK"])
                    if abs(need - got) > 0.001:
                        comp_bad.append(f"order {n}: component {comp['IDNRK']} reserves {got}, BOM needs {need}")
            plnnr = afko.get("PLNNR")
            ops = {o["VORNR"] for o in target.lookup("AFVC", "AUFNR", n)}
            if plnnr:
                if not target.get("PLKO", (plnnr,)):
                    route_bad.append(f"order {n}: routing {plnnr} missing in target")
                else:
                    want = {o["VORNR"] for o in target.lookup("PLPO", "PLNNR", plnnr)}
                    if ops != want:
                        route_bad.append(f"order {n}: operations {sorted(ops)} differ from routing {plnnr} operations {sorted(want)}")
            confs = target.lookup("AFRU", "AUFNR", n)
            for c in confs:
                if c["VORNR"] not in ops:
                    conf_bad.append(f"order {n}: confirmation {c['RUECK']} is for operation {c['VORNR']}, which the order does not have")
            received = sum(a["WEMNG"] for a in afpo)
            if ops and received > 0 and not confs:
                conf_bad.append(f"order {n}: {received} received but no operation was confirmed")
            elif confs and ops:
                last = max(ops)
                yielded = sum(c["LMNGA"] for c in confs if c["VORNR"] == last)
                if received > 0 and abs(yielded - received) > 0.001:
                    conf_bad.append(f"order {n}: yield confirmed at the last operation {yielded} != received quantity {received}")
            lines = target.lookup(item_t, "AUFNR", n)
            for bwart in ("101", "261"):
                sel = [l for l in lines if l["BWART"] == bwart]
                if not sel:
                    continue
                if bwart == "101":
                    got, want = sum(l["MENGE"] for l in sel), sum(a["WEMNG"] for a in afpo)
                    if abs(got - want) > 0.001:
                        move_bad.append(f"order {n}: goods receipts {got} != received quantity {want}")
                else:
                    for mat in {l["MATNR"] for l in sel}:
                        got = sum(l["MENGE"] for l in sel if l["MATNR"] == mat)
                        want = sum(r["ENMNG"] for r in resb if r["MATNR"] == mat)
                        if abs(got - want) > 0.001:
                            move_bad.append(f"order {n}: goods issues of {mat} {got} != withdrawn quantity {want}")
        for inst in mdocs:
            for ln in inst.rows.get(item_t, []):
                if ln.get("AUFNR") and not target.get("AUFK", (ln["AUFNR"],)):
                    move_bad.append(f"material document {inst.key}: order {ln['AUFNR']} missing in target")
        checks.append(_chk("business", "BUS-PP-STRUCT", "Production orders are complete (header, item) in the target", not struct_bad,
                           f"{len(struct_bad)} incomplete", struct_bad))
        checks.append(_chk("business", "BUS-PP-BOM", "The BOM of every loaded production order exists in the target", not bom_bad,
                           f"{len(bom_bad)} missing", bom_bad))
        checks.append(_chk("business", "BUS-PP-COMPONENTS", "Component reservations equal the BOM explosion for the order quantity", not comp_bad,
                           f"{len(comp_bad)} deviations", comp_bad))
        checks.append(_chk("business", "BUS-PP-ROUTING", "Order operations are a complete copy of the order's routing, and the routing exists in the target", not route_bad,
                           f"{len(route_bad)} inconsistent", route_bad))
        checks.append(_chk("business", "BUS-PP-CONFIRM", "Confirmations belong to the order's operations and the last operation's yield equals the received quantity", not conf_bad,
                           f"{len(conf_bad)} inconsistent", conf_bad))
        checks.append(_chk("business", "BUS-PP-MOVEMENTS", "Goods movements agree with order progress and reference existing orders", not move_bad,
                           f"{len(move_bad)} inconsistent", move_bad))

    # flight demo model (evaluated against the TARGET): references must resolve. No quantity invariants are asserted: real demo data does not keep them.
    flights = docs("FLIGHT")
    if flights:
        ref_bad = []
        for inst in flights:
            f = inst.rows["SFLIGHT"][0]
            if not target.get("SCARR", (f["CARRID"],)):
                ref_bad.append(f"flight {inst.key}: airline {f['CARRID']} missing in target")
            if not target.get("SPFLI", (f["CARRID"], f["CONNID"])):
                ref_bad.append(f"flight {inst.key}: connection {f['CARRID']}/{f['CONNID']} missing in target")
            for b in target.lookup("SBOOK", "CARRID", f["CARRID"]):
                if b["CONNID"] == f["CONNID"] and b["FLDATE"] == f["FLDATE"] and not target.get("SCUSTOM", (b["CUSTOMID"],)):
                    ref_bad.append(f"flight {inst.key}: booking {b['BOOKID']} refers to customer {b['CUSTOMID']}, missing in target")
            have = {(b["BOOKID"]) for b in target.lookup("SBOOK", "CARRID", f["CARRID"]) if b["CONNID"] == f["CONNID"] and b["FLDATE"] == f["FLDATE"]}
            if have != {b["BOOKID"] for b in inst.rows.get("SBOOK", [])}:
                ref_bad.append(f"flight {inst.key}: the target holds a different set of bookings than was loaded")
        checks.append(_chk("business", "BUS-FLIGHT-REFS", "Flights have their airline, connection and customers in the target, and all their bookings", not ref_bad,
                           f"{len(ref_bad)} inconsistent", ref_bad))

    # quality management (evaluated against the TARGET): references and completeness only. Usage decisions are human decisions: no invariant between
    # results and decision is asserted.
    lots = docs("INSPECTION_LOT")
    if lots:
        qm_bad = []
        for inst in lots:
            h = inst.rows["QALS"][0]
            if not target.get("MARA", (h["MATNR"],)):
                qm_bad.append(f"lot {inst.key}: material {h['MATNR']} missing in target")
            if h.get("AUFNR") and not target.get("AUFK", (h["AUFNR"],)):
                qm_bad.append(f"lot {inst.key}: production order {h['AUFNR']} missing in target")
            for t in ("QAMV", "QASR", "QAVE"):
                got = len(target.lookup(t, "PRUEFLOS", inst.key))
                if got != len(inst.rows.get(t, [])):
                    qm_bad.append(f"lot {inst.key}: {t} has {got} rows in the target, {len(inst.rows.get(t, []))} were loaded")
            chars = {c["MERKNR"] for c in target.lookup("QAMV", "PRUEFLOS", inst.key)}
            for r in target.lookup("QASR", "PRUEFLOS", inst.key):
                if r["MERKNR"] not in chars:
                    qm_bad.append(f"lot {inst.key}: a result refers to characteristic {r['MERKNR']}, which the lot does not have")
        checks.append(_chk("business", "BUS-QM-REFS", "Inspection lots are complete in the target and refer to existing materials, orders and characteristics", not qm_bad,
                           f"{len(qm_bad)} inconsistent", qm_bad))

    # HR master data (evaluated against the TARGET)
    employees = docs("EMPLOYEE")
    if employees:
        hr_bad = []
        for inst in employees:
            if not target.get("PA0003", (inst.key,)):
                hr_bad.append(f"employee {inst.key}: core record missing in target")
                continue
            for t, link in (("PA0001", "PERNR"), ("PA0002", "PERNR"), ("PA0006", "PERNR"), ("PA0008", "PERNR"), ("PA0009", "PERNR")):
                if len(target.lookup(t, "PERNR", inst.key)) != len(inst.rows.get(t, [])):
                    hr_bad.append(f"employee {inst.key}: {t} has {len(target.lookup(t, 'PERNR', inst.key))} rows in the target, {len(inst.rows.get(t, []))} were loaded")
            if not inst.rows.get("PA0001") or not inst.rows.get("PA0002"):
                hr_bad.append(f"employee {inst.key}: organizational assignment or personal data missing in the source")
        checks.append(_chk("business", "BUS-HR-REFS", "Every employee has the same infotype records in the target as were loaded", not hr_bad,
                           f"{len(hr_bad)} inconsistent", hr_bad))
        from ..security.authz import hr_masking_violations
        hv = hr_masking_violations(plan, masking.policy)
        checks.append(_chk("security", "SEC-HR-ANON", "HR fields were masked by per-run anonymization (never stable pseudonyms)", not hv, f"{len(hv)} violation(s)", hv))

    # ---------------- security ----------------
    cov = masking.coverage(required_sensitive)
    checks.append(_chk("security", "SEC-MASK-COVERAGE", "All discovered sensitive fields are covered by a masking rule",
                       cov["complete"], f"{cov['covered']}/{cov['required']} covered", [f"{m['table']}.{m['field']}" for m in cov["missing"]]))
    resid = []
    for (t, f), _ in masking.stats.items():
        for k in [k for k in per_table.get(t, [])]:
            tr = target.get(*_split(k))
            sr = source.get(*_split(k))
            if tr and sr and sr.get(f) not in (None, "", 0) and tr.get(f) == sr[f]:
                resid.append(f"{t}.{f} {k}")
    checks.append(_chk("security", "SEC-RESIDUAL", "No original sensitive value remains in masked fields", not resid,
                       f"{len(resid)} unmasked values", resid))
    loaded_rows: dict[str, list[dict]] = {}
    for k in run.expected:
        t, key = _split(k)
        r = target.get(t, key)
        if r:
            loaded_rows.setdefault(t, []).append(r)
    unprotected = [x for x in discover_sensitive(loaded_rows) if (x["table"], x["field"]) not in masking.policy.fields()]
    checks.append(_chk("security", "SEC-PII-SCAN", "Pattern scan finds no PII in unprotected fields", not unprotected,
                       f"{len(unprotected)} fields", [f"{x['table']}.{x['field']} ({x['category']})" for x in unprotected]))
    active = [o["name"] for o in target.outbound_interfaces() if o.get("active")]
    checks.append(_chk("security", "SEC-OUTBOUND", "Target outbound integrations are inactive", not active,
                       f"{len(active)} active", active))
    rep = masking.report()
    checks.append(_chk("security", "SEC-PROTECTION-CLASS",
                       "Protection class disclosed: " + ", ".join(rep["protection_classes"]), True,
                       "reversible tokens present" if rep["reversible"] else "no reversible tokens"))

    fails = [c for c in checks if c["status"] == "fail"]
    by_cat = {c: {"pass": 0, "fail": 0, "warn": 0} for c in ("technical", "business", "security")}
    for c in checks:
        by_cat[c["category"]][c["status"]] += 1
    gate = "RELEASED" if not fails and run.status == "COMPLETED" else "HELD"
    return {"checks": checks, "summary": by_cat, "failed": [c["id"] for c in fails], "release": gate,
            "release_rule": "Environment is released only when every technical, business and security check passes.",
            "masking": rep, "simulated": True}

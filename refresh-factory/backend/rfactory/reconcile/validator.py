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

    # ---------------- security ----------------
    cov = masking.coverage(required_sensitive)
    checks.append(_chk("security", "SEC-MASK-COVERAGE", "All discovered sensitive fields are covered by a masking rule",
                       cov["complete"], f"{cov['covered']}/{cov['required']} covered", [f"{m['table']}.{m['field']}" for m in cov["missing"]]))
    resid = []
    for (t, f), _ in masking.stats.items():
        for k in [k for k in per_table.get(t, [])]:
            tr = target.get(*_split(k))
            sr = source.get(*_split(k))
            if tr and sr and isinstance(sr.get(f), str) and sr[f] and tr.get(f) == sr[f]:
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

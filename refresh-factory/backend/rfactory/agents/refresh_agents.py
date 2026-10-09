"""The twelve refresh agents.

Every agent is a plain function `(svc, params, report) -> None` that READS platform state and writes findings and
evidence-backed recommendations into `report`. None of them mutates anything; they are handed the service only to read.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from ..dependency.planner import Planner
from ..selective.manifest import Scope
from . import advisors
from .framework import AgentReport, AgentSpec, ev

NOTE_SIM = "All inputs come from the simulated SAP adapters; nothing here has been validated against a real landscape."


def _project(svc, params):
    from ..service import NotFound
    pid = params.get("project_id")
    if not pid:
        raise ValueError("project_id is required")
    try:
        return svc.project(pid)
    except NotFound:
        raise ValueError(f"unknown project {pid}")


# ---------------------------------------------------------------- 1 landscape discovery
def landscape_discovery(svc, params, rep: AgentReport) -> None:
    rep.subject = "SAP landscape"
    for s in svc.systems.values():
        a = svc.adapters[s.id]
        tag = f"system:{s.label}"
        if not s.owner:
            rep.recommend(f"Assign an owner to {s.label}", "Without an owner nobody can be contacted about overwriting this system.", "medium", "high",
                          "direct field check", ["The system has no registered owner."], [ev("landscape", tag, "owner is empty")])
        if s.is_production and s.writable_target_allowed:
            rep.recommend(f"Lock {s.label} against modification", "Production is extraction-only; the write-target flag should be off as defence in depth.",
                          "high", "high", "direct field check",
                          ["Writes to production are already blocked structurally; the flag is still on, which weakens defence in depth."],
                          [ev("landscape", tag, "writable_target_allowed=true on a production system")], "lock_system", {"system_id": s.id})
        if not s.is_production:
            active = [o.get("name") for o in a.outbound_interfaces() if o.get("active")]
            if active:
                rep.finding("warning", f"{s.label}: {len(active)} outbound interface(s) active - refreshed data could trigger external systems.",
                            [ev("adapter", tag, ", ".join(map(str, active[:5])))])
            ref = svc.postcopy.assess(s.id)
            if ref["active_production_references"]:
                rep.finding("warning", f"{s.label}: {ref['active_production_references']} active reference(s) to production hosts (post-copy needed).",
                            [ev("postcopy.assess", tag, f"{ref['active_production_references']} active production references")])
    for c in svc.lean.clients():
        if c.get("status") == "EXPIRED":
            rep.recommend(f"Decommission expired lean client {c['label']}", "The client is past its lease and locked; it still occupies a system.",
                          "low", "high", "lease expiry", ["The build's lease has expired."], [ev("lean", c["id"], f"status={c['status']}")],
                          "decommission_client", {"build_id": c["id"]})
    combos = [x for x in svc.refresh_combinations() if x["selective"]]
    rep.artifacts["systems"] = [{"id": s.id, "label": s.label, "family": s.family, "owner": s.owner, "production": s.is_production} for s in svc.systems.values()]
    rep.artifacts["selective_pairs"] = len(combos)
    rep.summary = f"{len(svc.systems)} systems across {len({s.family for s in svc.systems.values()})} product families; {len(combos)} valid selective-refresh pairs."
    rep.limitations.append(NOTE_SIM)


# ---------------------------------------------------------------- 2 refresh strategy
def refresh_strategy(svc, params, rep: AgentReport) -> None:
    p = _project(svc, params)
    if not p.plan:
        raise ValueError("build the plan first: strategy needs the real scope size")
    rep.subject = p.name
    s = svc.plan_summary(p.id)
    cfg_gaps = [i for i in p.plan.issues if i.code.startswith("CONFIG") or "customizing" in i.message.lower()]
    gb = float(params.get("source_gb", 0) or 0)
    adv = advisors.refresh_strategy(source_rows=s["source_total_rows"], scope_rows=s["total_rows"], source_gb=gb,
                                    config_change_needed=bool(cfg_gaps), freshness_days=int(params.get("freshness_days", 30)),
                                    masking_required=True)
    base = [ev("plan", p.id, f"{s['total_rows']} of {s['source_total_rows']} source rows in scope"),
            ev("plan", p.id, f"{len(cfg_gaps)} customizing gap(s) in the plan")]
    if gb == 0:
        rep.limitations.append("source_gb was not supplied, so the size rules are inactive (the simulation has no real DB size).")
    rep.limitations.append("Scores come from fixed rules; they are not a trained model and have not been calibrated.")
    rep.artifacts["scores"] = adv["scores"]
    rep.recommend(f"Use {adv['recommendation'].replace('_', ' ')}", "; ".join(adv["rationale"]) or "Highest rule score.", "high" if cfg_gaps else "medium",
                  "medium", "rule score margin over runner-up: " + str(sorted(adv["scores"].values())[-1] - sorted(adv["scores"].values())[-2]),
                  adv["rationale"] or ["selective is the default for a bounded scope"], base)
    d = [x for x in svc.delta.scenarios.values() if x.target_id == p.target_id]
    if not d and int(params.get("freshness_days", 30)) <= 7:
        rep.recommend("Consider a delta refresh scenario", "A short freshness window is better served by incremental refresh than repeated full selective copies.",
                      "low", "medium", "freshness_days<=7", ["Freshness need is 7 days or less and no delta scenario targets this system."],
                      base + [ev("delta", p.target_id, "no scenarios for this target")])
    rep.summary = f"Recommended: {adv['recommendation']} (scope {s['total_rows']} rows = {s['total_rows'] / max(1, s['source_total_rows']):.1%} of source)."


# ---------------------------------------------------------------- 3 business dependency
def business_dependency(svc, params, rep: AgentReport) -> None:
    p = _project(svc, params)
    if not p.plan:
        raise ValueError("build the plan first")
    rep.subject = p.name
    plan = p.plan
    origin = Counter(i.origin for i in plan.instances.values())
    rep.artifacts["origin_counts"] = dict(origin)
    rep.artifacts["config_prerequisites"] = {k: sorted(v) for k, v in plan.config_refs.items()}
    rep.summary = (f"{origin.get('ROOT', 0)} selected objects pulled in {origin.get('REQUIRED', 0)} required and {origin.get('DOWNSTREAM', 0)} downstream objects.")
    ratio = origin.get("REQUIRED", 0) / max(1, origin.get("ROOT", 0))
    if ratio >= 1:
        rep.finding("info", f"Each selected object drags in {ratio:.1f} required objects on average (masters, partners, materials).",
                    [ev("plan", p.id, str(dict(origin)))])
    ccs = {i.key.split("/")[0] for i in plan.instances.values() if i.type in ("COMPANY_CODE",)}
    if len(plan.config_refs.get("COMPANY_CODE", set())) > len(p.manifest.scope.company_codes) > 0:
        extra = sorted(plan.config_refs["COMPANY_CODE"] - set(p.manifest.scope.company_codes))
        rep.finding("warning", f"Cross-company dependency: the plan needs company code(s) {extra} that are outside the stated scope.",
                    [ev("plan", p.id, f"config_refs COMPANY_CODE={sorted(plan.config_refs['COMPANY_CODE'])}")])
    bad = [i for i in plan.issues if i.severity != "info"]
    for i in bad[:10]:
        rep.finding(i.severity, i.message, [ev("planner", i.instance or p.id, i.code)])
    if plan.cycles:
        rep.finding("blocking", f"{len(plan.cycles)} dependency cycle(s) in the plan.", [ev("planner", p.id, str(plan.cycles[0]))])
    missing = [t for t in ("DELIVERY", "BILLING", "FI_DOCUMENT") if t not in p.manifest.include_downstream]
    if missing and p.manifest.scope.object_type == "SALES_ORDER":
        types = list(p.manifest.include_downstream) + missing
        rep.recommend("Include the full document flow", f"Orders without {', '.join(missing)} leave tests that start from delivery or invoicing without data.",
                      "low", "medium", "downstream types absent from the manifest",
                      [f"Downstream types {missing} are not included."], [ev("manifest", p.id, f"include_downstream={p.manifest.include_downstream}")],
                      "set_scope", {"project_id": p.id, "include_downstream": types})
    rep.limitations.append("Dependencies are exactly those encoded in the object registry; relationships it does not know cannot be found.")


# ---------------------------------------------------------------- 4 selective scope optimisation
def scope_optimization(svc, params, rep: AgentReport) -> None:
    p = _project(svc, params)
    if not p.plan:
        raise ValueError("build the plan first")
    rep.subject = p.name
    min_frac = float(params.get("min_root_fraction", 0.5))
    base_rows = p.plan.summary()["total_rows"]
    roots0 = sum(1 for i in p.plan.instances.values() if i.origin == "ROOT")
    sc = p.manifest.scope
    variants = []
    if sc.date_from and sc.date_to:
        span = (sc.date_to - sc.date_from).days
        for frac in (0.5, 0.25):
            d = max(1, int(span * frac))
            variants.append((f"last {d} days", sc.model_copy(update={"date_from": sc.date_to - timedelta(days=d)}), p.manifest.include_downstream))
    view, reg = svc.source_view(p.source_id), svc.reg(p)
    results = []
    for name, scope, down in variants:
        pl = Planner(view, reg).build(p.manifest.model_copy(update={"scope": scope, "include_downstream": list(down)}))
        rows, roots = pl.summary()["total_rows"], sum(1 for i in pl.instances.values() if i.origin == "ROOT")
        results.append({"variant": name, "rows": rows, "roots": roots, "reduction": round(1 - rows / max(1, base_rows), 3),
                        "blocking": len(pl.blocking), "scope": scope, "down": list(down)})
    rep.artifacts["variants"] = [{k: v for k, v in r.items() if k not in ("scope", "down")} for r in results]
    rep.artifacts["baseline"] = {"rows": base_rows, "roots": roots0}
    ok = [r for r in results if not r["blocking"] and r["roots"] >= min_frac * roots0 and r["reduction"] > 0.1]
    if ok:
        b = max(ok, key=lambda r: r["reduction"])
        rep.recommend(f"Narrow scope: {b['variant']}", f"Cuts {b['reduction']:.0%} of rows ({base_rows} -> {b['rows']}) while keeping {b['roots']} of {roots0} selected objects.",
                      "low", "medium", "dry-run planning of each variant against the live source (read-only)",
                      [f"Dry run: {b['rows']} rows vs {base_rows}", f"Selected objects kept: {b['roots']}/{roots0} (minimum {min_frac:.0%} required)"],
                      [ev("planner-dry-run", p.id, f"{b['variant']}: rows={b['rows']} roots={b['roots']} blocking={b['blocking']}")],
                      "set_scope", {"project_id": p.id, "scope": b["scope"].model_dump(mode="json"), "include_downstream": b["down"]})
        rep.summary = f"Best reduction {b['reduction']:.0%} with '{b['variant']}'."
    else:
        rep.summary = "No variant reduces volume by >10% without losing too many selected objects or introducing blockers."
    rep.limitations.append("Which data a test really needs is unknown to the platform: confirm the reduced scope with the test owner.")


# ---------------------------------------------------------------- 5 masking recommendation
def masking_recommendation(svc, params, rep: AgentReport) -> None:
    p = _project(svc, params)
    rep.subject = p.name
    st = svc.masking_state(p.id)
    tgt = svc.system(p.target_id)
    adv = advisors.masking_recommendation(svc.required_sensitive.get(p.id, []), p.masking_policy.fields() if p.masking_policy else set(), tgt.role.value)
    rep.artifacts["coverage"] = {"discovered": len(st["discovered"]), "uncovered": st["uncovered"], "recommended_mode": adv["recommended_mode"]}
    if adv["add_rules"]:
        rep.recommend(f"Add {len(adv['add_rules'])} masking rule(s)", "Sensitive fields found in the copied rows are not covered by the active policy; reconciliation would hold the run.",
                      "critical", "high", "discovery by data-element catalogue and value patterns on the actual rows",
                      adv["rationale"][:1], [ev("discovery", f"{r['table']}.{r['field']}", r["reason"]) for r in adv["add_rules"][:12]],
                      "add_masking_rules", {"project_id": p.id, "rules": [{k: r[k] for k in ("table", "field", "strategy", "category")} for r in adv["add_rules"]]})
    elif p.plan:
        rep.finding("ok", "Every discovered sensitive field is covered.", [ev("masking", p.id, "uncovered=0")])
    if tgt.role.value in ("SBX", "TRN", "UAT") and p.masking_policy and any(r.mode.value == "PSEUDONYMIZE" for r in p.masking_policy.rules):
        rep.recommend("Consider ANONYMIZE for this widely accessible target", "Pseudonymized data stays re-identifiable with the key; a sandbox/training audience does not need that.",
                      "low", "medium", "target role", [adv["rationale"][1]], [ev("system", tgt.label, f"role={tgt.role.value}")])
    pii = [(d["table"], d["field"]) for d in svc.required_sensitive.get(p.id, []) if d.get("category") in ("birthdate", "postal_code", "city", "country")]
    if pii:
        rep.finding("info", "Quasi-identifiers present (they can re-identify in combination even when direct identifiers are masked).",
                    [ev("discovery", f"{t}.{f}", "quasi-identifier") for t, f in pii[:6]])
    rep.summary = f"{st['uncovered']} of {len(st['discovered'])} discovered sensitive fields lack a rule."
    rep.limitations.append("Discovery sees only the fields that are in the DDIC subset and in the rows being copied; free text is not scanned.")


# ---------------------------------------------------------------- 6 refresh scheduling
def refresh_scheduling(svc, params, rep: AgentReport) -> None:
    rep.subject = "delta refresh scenarios"
    scs = list(svc.delta.scenarios.values())
    rep.summary = f"{len(scs)} delta scenario(s) reviewed."
    by_target = Counter(s.target_id for s in scs if s.schedule)
    for s in scs:
        a = svc.adapters[s.source_id]
        tag = f"delta:{s.name}"
        pending = max(0, len(a.changelog) - (s.watermark or 0)) if s.watermark is not None else None
        if s.status == "ATTENTION" or s.pending:
            rep.finding("warning", f"{s.name}: needs attention (unacknowledged held run or failed run).", [ev("delta", s.id, f"status={s.status}")])
        if not s.schedule:
            rep.finding("info", f"{s.name}: no schedule - runs only when triggered manually.", [ev("delta", s.id, "schedule=null")])
        elif by_target[s.target_id] > 1:
            rep.finding("warning", f"{s.name}: {by_target[s.target_id]} scheduled scenarios write to the same target; runs may contend.",
                        [ev("delta", s.id, f"target {s.target_id} has {by_target[s.target_id]} scheduled scenarios")])
        if pending and pending > 50 and s.status == "APPROVED":
            rep.recommend(f"Run {s.name} soon", f"{pending} source changes are waiting behind the watermark.", "medium", "medium",
                          "change-log position minus watermark", [f"{pending} unprocessed change-log entries."],
                          [ev("delta", s.id, f"watermark={s.watermark}, source log={len(a.changelog)}")])
        blocked = sum(1 for h in s.history if h.get("status") == "BLOCKED")
        if blocked >= 2:
            rep.finding("warning", f"{s.name}: {blocked} of {len(s.history)} runs were blocked.", [ev("delta", s.id, "history")])
        if s.history and len(s.history) >= s.full_sweep_every * 2 and s.full_sweep_every > 8:
            rep.recommend(f"Sweep {s.name} more often", "Modifications that the change log cannot see (direct DB writes, archived documents) accumulate between full sweeps.",
                          "low", "low", "sweep interval vs run count", [f"{len(s.history)} runs with a sweep every {s.full_sweep_every}."],
                          [ev("delta", s.id, f"full_sweep_every={s.full_sweep_every}")], "adjust_delta_sweep", {"scenario_id": s.id, "full_sweep_every": 4})
        if s.stale:
            rep.finding("warning", f"{s.name}: {len(s.stale)} object(s) are stale (source changed, change not applied).", [ev("delta", s.id, f"stale={len(s.stale)}")])
    for d in svc.tdm.datasets.values():
        if d.state == "RESERVED" and d.reserved_until is not None:
            rep.finding("info", f"Dataset {d.name} is reserved by {d.reserved_by}.", [ev("tdm", d.id, str(d.reserved_until))])
    rep.limitations.append("No scheduler runs in this MVP: due times are computed, an external scheduler must trigger runs.")


# ---------------------------------------------------------------- 7 target conflict analysis
def conflict_analysis(svc, params, rep: AgentReport) -> None:
    p = _project(svc, params)
    if not p.report:
        raise ValueError("analyze conflicts first")
    rep.subject = p.name
    c = svc.conflicts(p.id)
    kinds = Counter(f["type"] for f in c["findings"])
    rep.artifacts["by_type"] = dict(kinds)
    tgt = svc.system(p.target_id)
    rep.summary = f"{len(c['findings'])} finding(s), {len(p.report.blocking)} blocking, target owner {tgt.owner or 'unknown'}."
    adv = svc.suggest_policies(p.id)
    rep.artifacts["policy_alternatives"] = adv["alternatives"]
    if p.report.blocking:
        best = next((a for a in adv["alternatives"] if a["policy"] == adv["recommended_policy"]), None)
        if best and not best["blocking"] and best["policy"] != "current":
            pol = best["settings"]
            rep.recommend(f"Switch conflict policy to '{best['policy']}'", f"Clears all blocking findings; {best['executable_objects']} objects remain loadable, {best['quarantined']} quarantined.",
                          "high", "medium", "re-running the conflict analysis under each alternative policy (read-only)",
                          adv["rationale"], [ev("conflicts", p.id, f"{len(p.report.blocking)} blocking under current policy")] +
                          [ev("what-if", p.id, f"{a['policy']}: blocking={a['blocking']} executable={a['executable_objects']}") for a in adv["alternatives"]],
                          "set_conflict_policy", {"project_id": p.id, "policy": {k: v for k, v in pol.items()}})
        else:
            rep.finding("blocking", "No automatic policy clears the blockers; they need human decisions.", [ev("conflicts", p.id, "all alternatives still blocked")])
    for f in c["findings"]:
        if f["type"] in ("DUPLICATE_DIFFERENT",) and f["severity"] != "info" and len(rep.findings) < 12:
            rep.finding(f["severity"], f["message"], [ev("conflicts", f["instance"] or p.id, f["type"])])
    n = sum(1 for f in c["findings"] if f["type"] == "DUPLICATE_DIFFERENT")
    if n:
        rep.recommend("Contact the target owner before overwriting", f"{n} object(s) exist in the target with different content; only an approved exception may REPLACE them.",
                      "medium", "high", "conflict type count", ["REPLACE is never recommended automatically."],
                      [ev("system", tgt.label, f"owner={tgt.owner or 'unknown'}")] + [ev("conflicts", p.id, f"{n} DUPLICATE_DIFFERENT")])
    rep.limitations.append("The agent cannot know whether target-side differences are valuable test data; that is the owner's call.")


# ---------------------------------------------------------------- 8 post-copy automation
def postcopy_automation(svc, params, rep: AgentReport) -> None:
    tid = params.get("system_id")
    if not tid:
        raise ValueError("system_id is required")
    s = svc.system(tid)
    rep.subject = s.label
    pcs = svc.postcopy
    a = pcs.assess(tid)
    rep.artifacts["assessment"] = {c: v["active_production_refs"] for c, v in a["categories"].items() if v["active_production_refs"]}
    rep.summary = f"{a['active_production_references']} active production reference(s) on {s.label}."
    if s.is_production:
        rep.finding("blocking", "Production system: post-copy automation never runs here.", [ev("system", s.label, "role=PRD")])
        return
    profs = [p for p in pcs.profiles.values() if p.system_id == tid]
    good = [p for p in profs if p.status == "APPROVED" and p.approval and p.approval["hash"] == p.hash()]
    if not a["active_production_references"] and not profs:
        rep.recommend("Capture a clean target profile now", "The system is clean today; a profile taken now is what a refresh will later be restored from.", "medium", "high",
                      "assessment shows no production references", ["There is no stored profile for this system."],
                      [ev("postcopy.assess", s.label, "0 active production references"), ev("postcopy", s.label, "no profiles")],
                      "capture_profile", {"system_id": tid, "name": f"{s.sid} baseline"})
    elif a["active_production_references"] and not good:
        rep.finding("blocking", "Production references exist and no approved profile is available to restore from.", [ev("postcopy", s.label, f"profiles={len(profs)}, approved={len(good)}")])
    elif a["active_production_references"] and good:
        pr = good[-1]
        need = pcs.assess(tid, pr.id).get("tasks_needed", [])
        labels = sorted({t["approval"] for t in need if t["approval"] != "none"})
        rep.recommend(f"Create a post-copy run ({len(need)} task(s) need action)", f"Restores {s.sid} from approved profile '{pr.name}'; approvals required from: {', '.join(labels) or 'none'}.",
                      "high", "high", "assessment against approved profile",
                      [f"{a['active_production_references']} active references to production hosts.", "Creating a run does not execute it; execution needs the listed approvals."],
                      [ev("postcopy.assess", s.label, f"{t['id']}: {t['violations'][0] if t['violations'] else ''}") for t in need[:10]],
                      "create_postcopy_run", {"target_id": tid, "profile_id": pr.id})
    if a["privileged_users"]:
        rep.finding("warning", f"{len(a['privileged_users'])} unlocked user(s) hold SAP_ALL.", [ev("tech", s.label, ", ".join(a["privileged_users"][:5]))])
    rep.limitations.append("Technical state is simulated (tech-state model); no real RFC destinations, jobs or certificates were inspected.")


# ---------------------------------------------------------------- 9 reconciliation analysis
ROOT_CAUSE = {
    "TECH-COUNT": ("rows missing in target", "A load step did not complete or was filtered out by conflict handling; compare quarantined/skipped objects."),
    "TECH-CHECKSUM": ("content differs from the masked staging copy", "The target was modified after load or masking is not deterministic across steps."),
    "TECH-DUP": ("duplicate keys", "A key collided with an existing target row; review the conflict policy for that object type."),
    "TECH-REFS": ("missing referenced master/config", "A required object was skipped or quarantined, or customizing is missing in the target."),
    "TECH-STRUCT": ("structure/type mismatch", "Source and target DDIC differ; check release/patch level between systems."),
    "BUS-DOCFLOW": ("document flow incomplete", "Predecessor/successor documents were out of scope; include downstream types."),
    "BUS-CHAIN": ("sales chain broken", "A delivery/billing document is missing; include downstream types or widen the date window."),
    "BUS-TOTALS": ("header/item totals mismatch", "Rows from different points in time were copied; re-extract."),
    "BUS-FI-BALANCE": ("accounting document does not balance", "Some FI lines were out of scope; include the FI document type."),
    "BUS-NUMBER-RANGE": ("number range below loaded documents", "Raise target number-range levels before go-live."),
    "BUS-QUARANTINE": ("a loaded object depends on a quarantined one", "Resolve the quarantined object or exclude its dependants."),
    "SEC-MASK-COVERAGE": ("sensitive field without a masking rule", "Add the rule and re-run; this is a release blocker."),
    "SEC-RESIDUAL": ("original value found in a masked field", "Masking rule is ineffective for that field/mode; fix before any release."),
    "SEC-PII-SCAN": ("PII-like values in an unprotected field", "Extend the masking policy to the field."),
}


def reconciliation_analysis(svc, params, rep: AgentReport) -> None:
    runs = [r for r in svc.runs.values() if r.reconciliation]
    if params.get("run_id"):
        runs = [r for r in runs if r.id == params["run_id"]]
        if not runs:
            raise ValueError("unknown run or run has no reconciliation")
    rep.subject = f"{len(runs)} reconciled run(s)"
    fails: Counter = Counter()
    for r in runs:
        for c in r.reconciliation["checks"]:
            if c["status"] != "pass":
                fails[c["id"].rsplit("-", 1)[0] if c["id"].startswith("TECH-COUNT") else c["id"]] += 1
    rep.artifacts["failed_checks"] = dict(fails)
    rep.summary = f"{sum(1 for r in runs if r.release == 'HELD')} held, {sum(1 for r in runs if r.release == 'RELEASED')} released."
    for cid, n in fails.most_common():
        cause = ROOT_CAUSE.get(cid)
        samples = [(r.id, c) for r in runs for c in r.reconciliation["checks"] if c["id"].startswith(cid) and c["status"] != "pass"]
        evid = [ev("reconciliation", rid, f"{c['id']}: {c.get('detail', '')}") for rid, c in samples[:6]]
        if cause:
            sec = cid.startswith("SEC")
            rep.recommend(f"Resolve {cid}: {cause[0]}", cause[1], "critical" if sec else "high", "medium", "static rule table keyed by check id; the true cause may differ",
                          [f"{cid} failed in {n} run(s)."], evid, "decision")
        else:
            rep.finding("warning", f"{cid} failed in {n} run(s); no remediation rule for it.", evid)
        if n > 1:
            rep.finding("warning", f"{cid} is recurring ({n} runs) - a configuration cause is more likely than a one-off fault.", evid)
    for r in runs:
        if r.release == "HELD" and r.status == "COMPLETED":
            rep.recommend(f"Review held run {r.id}", "The release gate held this run; data must not be handed to testers until the failed checks are fixed or rolled back.",
                          "high", "high", "release gate", ["Release status is HELD."], [ev("run", r.id, f"release={r.release}, failed={r.reconciliation['failed']}")],
                          "rollback_run", {"run_id": r.id})
    rep.limitations.append("Root-cause hints are a static lookup, not diagnosis; check the cited run events before acting.")


# ---------------------------------------------------------------- 10 performance optimisation
def performance_optimization(svc, params, rep: AgentReport) -> None:
    runs = [r for r in svc.runs.values() if r.status in ("COMPLETED", "FAILED")]
    rep.subject = f"{len(runs)} execution(s)"
    rep.limitations.append("Observed in a simulation: durations are in-process timings and row counts, NOT SAP benchmark figures. Do not size real jobs from them.")
    if not runs:
        rep.summary = "No executed runs yet; nothing to analyse."
        return
    steps = Counter()
    for r in runs:
        for s in r.steps:
            if s.get("started") and s.get("finished"):
                from datetime import datetime
                steps[s["name"]] += (datetime.fromisoformat(s["finished"]) - datetime.fromisoformat(s["started"])).total_seconds()
    tot = sum(steps.values()) or 1e-9
    rep.artifacts["step_seconds"] = {k: round(v, 4) for k, v in steps.most_common()}
    top = steps.most_common(1)[0] if steps else None
    retries = sum(r.attempts - 1 for r in runs if r.attempts > 1)
    big = Counter()
    for p in svc.projects.values():
        if p.plan:
            for t, n in p.plan.summary()["tables"].items():
                big[t] = max(big[t], n)
    rep.artifacts["largest_tables"] = dict(big.most_common(5))
    rep.summary = f"Dominant step: {top[0]} ({top[1] / tot:.0%} of observed time)." if top else "No timed steps."
    if top and top[1] / tot > 0.5:
        rep.recommend(f"Look at the '{top[0]}' step first", "More than half of observed run time is spent in one step; that is where a real-landscape test would pay off most.",
                      "low", "low", "share of observed time in the simulation", [f"{top[0]} = {top[1] / tot:.0%} of time over {len(runs)} run(s)."],
                      [ev("runs", ",".join(r.id for r in runs[:3]), f"{k}={v:.3f}s") for k, v in steps.most_common(3)])
    if retries:
        rep.finding("warning", f"{retries} retried attempt(s) across runs: transient errors cost time.", [ev("runs", "all", f"attempts>1 on {sum(1 for r in runs if r.attempts > 1)} run(s)")])
    if big:
        t, n = big.most_common(1)[0]
        if n >= 5000:
            rep.recommend(f"Consider partitioned extraction for {t}", "A single table dominates volume.", "low", "low", "table row count", [f"{t} has {n} rows in scope."], [ev("plan", t, f"{n} rows")])


# ---------------------------------------------------------------- 11 compliance verification
def compliance_verification(svc, params, rep: AgentReport) -> None:
    rep.subject = "control environment"
    controls: list[dict] = []

    def ctl(cid, name, ok, detail, evid=None):
        controls.append({"id": cid, "name": name, "status": "pass" if ok else "fail", "detail": detail})
        if not ok:
            rep.finding("blocking", f"{cid} {name}: {detail}", evid or [ev("control", cid, detail)])

    v = svc.audit.verify()
    ctl("C01", "Audit chain intact", v["valid"], f"{v['entries']} entries" if v["valid"] else f"broken at seq {v.get('broken_at')}")
    sod = [p for p in svc.projects.values() if p.approval and p.approval.get("by") in (p.created_by, p.last_editor, p.submitted_by)]
    ctl("C02", "Separation of duties on plan approvals", not sod, f"{len(sod)} violation(s)")
    prod_w = [s.label for s in svc.systems.values() if s.is_production and s.writable_target_allowed]
    ctl("C03", "Production not flagged writable", not prod_w, ", ".join(prod_w) or "all locked")
    unc = sum(svc.masking_state(p.id)["uncovered"] for p in svc.projects.values() if p.plan and p.status in ("APPROVED", "RUNNING", "COMPLETED"))
    ctl("C04", "Masking coverage on approved/executed plans", unc == 0, f"{unc} uncovered sensitive field(s)")
    bad = [r.id for r in svc.runs.values() if r.reconciliation and any(c["id"] == "SEC-RESIDUAL" and c["status"] != "pass" for c in r.reconciliation["checks"])]
    ctl("C05", "No residual PII in masked fields", not bad, f"{len(bad)} run(s) affected", [ev("run", b, "SEC-RESIDUAL failed") for b in bad[:5]])
    rel_bad = [r.id for r in svc.runs.values() if r.release == "RELEASED" and r.reconciliation and r.reconciliation.get("failed")]
    ctl("C06", "Released runs have no failed checks", not rel_bad, f"{len(rel_bad)} run(s)")
    unack = [s.name for s in svc.delta.scenarios.values() if s.status == "ATTENTION"]
    ctl("C07", "Held delta runs acknowledged", not unack, ", ".join(unack) or "none pending")
    from ..delta.engine import config_hash
    stale = [s.name for s in svc.delta.scenarios.values() if s.approval and s.approval.get("hash") != config_hash(s)]
    ctl("C08", "Standing delta approvals match current configuration", not stale, ", ".join(stale) or "all bound to current hash")
    over = [d.name for d in svc.tdm.datasets.values() if d.state == "AVAILABLE" and d.expires_at and d.expires_at < __import__("datetime").datetime.now(d.expires_at.tzinfo)]
    ctl("C09", "No datasets past retention", not over, f"{len(over)} overdue")
    ag = [e for e in svc.audit.entries() if e["actor"].startswith("refresh.") and e["action"] in ("plan.approved", "run.executed", "plan.approve")]
    ctl("C10", "Agent principals never approved or executed", not ag, f"{len(ag)} event(s)")
    nonprod_ifc = [s.label for s in svc.systems.values() if not s.is_production and any(o.get("active") for o in svc.adapters[s.id].outbound_interfaces())]
    ctl("C11", "Outbound interfaces inactive on non-production", not nonprod_ifc, ", ".join(nonprod_ifc) or "none active")
    refs = [s.label for s in svc.systems.values() if not s.is_production and svc.postcopy.assess(s.id)["active_production_references"]]
    ctl("C12", "Non-production systems free of active production references", not refs, ", ".join(refs) or "clean")
    full = [p.name for p in svc.projects.values() if p.manifest and not p.manifest.scope.dims() and not p.manifest.scope.explicit_keys and not (p.manifest.scope.date_from)]
    ctl("C13", "Data minimisation: no unbounded scopes", not full, ", ".join(full) or "all scopes bounded")
    unm = [f"{svc.system(p.target_id).label} (program {p.id}, phase {p.checkpoint + 1})" for p in svc.full.programs.values() if p.unmasked_target]
    ctl("C14", "No target holds unmasked production data", not unm, "; ".join(unm) or "none",
        [ev("fullrefresh", u, "target holds unmasked copied data until phase 11 completes") for u in unm])
    rep.artifacts["controls"] = controls
    f = sum(1 for c in controls if c["status"] == "fail")
    rep.summary = f"{len(controls) - f}/{len(controls)} controls pass."
    rep.limitations += ["Checks the platform's own state; it is not a substitute for audit of SAP authorisations or of the real landscape.",
                        "No control mapping to a specific regulation (GDPR articles, SOX) has been reviewed by compliance staff."]


# ---------------------------------------------------------------- 12 documentation
def documentation(svc, params, rep: AgentReport) -> None:
    kind, ref = params.get("kind", "project"), params.get("id")
    if not ref:
        raise ValueError("id is required")
    rep.subject = f"{kind} {ref}"
    lines: list[str] = []
    if kind == "project":
        p = _project(svc, {"project_id": ref})
        m = p.manifest
        lines = [f"# Refresh documentation: {p.name}", "", f"- Source: {svc.system(p.source_id).label}", f"- Target: {svc.system(p.target_id).label}",
                 f"- Status: {p.status}", f"- Scope: `{m.scope.model_dump(mode='json', exclude_defaults=True)}`" if m else "- No manifest"]
        if p.plan:
            s = p.plan.summary()
            lines += [f"- Objects: {s['instances']} ({s['by_origin']})", f"- Rows in scope: {s['total_rows']}"]
        if p.approval:
            lines.append(f"- Approved by {p.approval.get('by')} for manifest hash `{p.approval.get('hash', '')[:12]}`")
        for rid in p.runs:
            r = svc.runs[rid]
            lines.append(f"- Run {rid}: {r.status}, release {r.release}, loaded {len(r.loaded)}")
        evid = [ev("project", p.id, p.status)]
    elif kind == "postcopy_run":
        r = svc.postcopy.get_run(ref)
        lines = [f"# Post-copy run {r.id}", f"- Target: {svc.system(r.target_id).label}", f"- Status: {r.status}", ""] + \
                [f"- {t['id']} {t['name']}: {t['status']}" for t in r.tasks]
        evid = [ev("postcopy", r.id, r.status)]
    elif kind == "lean_build":
        b = svc.lean.get_build(ref)
        lines = [f"# Lean client build {b.name}", f"- Status: {b.status}", f"- Host: {svc.system(b.host_id).label}", ""] + \
                [f"- {p['name']}: {p['status']}" for p in b.phases] + [f"- Check {c['id']}: {c['status']}" for c in b.checks]
        evid = [ev("lean", b.id, b.status)]
    else:
        raise ValueError("kind must be project, postcopy_run or lean_build")
    audit = svc.audit.entries(resource=ref, limit=15)
    lines += ["", "## Audit trail (most recent)"] + [f"- {e['ts'][:19]} {e['actor']} {e['action']}" for e in audit]
    lines += ["", "_Generated from platform records; every statement above is a recorded fact. Simulated environment._"]
    rep.artifacts["markdown"] = "\n".join(lines)
    rep.summary = f"Documentation generated ({len(lines)} lines, {len(audit)} audit entries)."
    rep.finding("info", "Document generated from recorded facts; nothing was inferred.", evid)


def specs() -> list[AgentSpec]:
    P = lambda n, d, req=False: {"name": n, "description": d, "required": req}
    return [
        AgentSpec("landscape-discovery", 1, "Landscape Discovery", "Finds ownership gaps, writable production flags, active outbound interfaces, production references and expired clients.",
                  [], ["systems", "adapters", "postcopy assessment", "lean clients"], ["lock_system", "decommission_client"], landscape_discovery),
        AgentSpec("refresh-strategy", 2, "Refresh Strategy", "Compares selective, client copy and full system refresh for a planned project.",
                  [P("project_id", "Project with a built plan", True), P("source_gb", "Source DB size in GB (optional)"), P("freshness_days", "Required data freshness")],
                  ["plan", "source totals"], ["decision"], refresh_strategy),
        AgentSpec("business-dependency", 3, "Business Dependency", "Explains what the plan pulled in and why; flags cross-company and cycles.",
                  [P("project_id", "Project with a built plan", True)], ["plan", "registry"], ["set_scope"], business_dependency),
        AgentSpec("scope-optimization", 4, "Selective Scope Optimization", "Dry-runs narrower scopes against the read-only source and recommends the largest safe reduction.",
                  [P("project_id", "Project with a built plan", True), P("min_root_fraction", "Minimum share of selected objects to keep")],
                  ["plan", "read-only source"], ["set_scope"], scope_optimization),
        AgentSpec("masking-recommendation", 5, "Masking Recommendation", "Finds sensitive fields without a rule and suggests rules and mode.",
                  [P("project_id", "Project", True)], ["discovery", "masking policy"], ["add_masking_rules"], masking_recommendation),
        AgentSpec("refresh-scheduling", 6, "Refresh Scheduling", "Reviews delta scenarios: backlog, blocked runs, contention, sweep cadence.",
                  [], ["delta scenarios", "change log"], ["adjust_delta_sweep", "adjust_delta_schedule"], refresh_scheduling),
        AgentSpec("conflict-analysis", 7, "Target Conflict Analysis", "What-if over conflict policies; who to contact before overwriting.",
                  [P("project_id", "Project with conflicts analysed", True)], ["conflict report", "target"], ["set_conflict_policy", "replace_objects"], conflict_analysis),
        AgentSpec("postcopy-automation", 8, "Post-Copy Automation", "Assesses production references and proposes profile capture or a post-copy run.",
                  [P("system_id", "Target system", True)], ["tech state", "profiles"], ["capture_profile", "create_postcopy_run"], postcopy_automation),
        AgentSpec("reconciliation-analysis", 9, "Reconciliation Analysis", "Groups failed checks, recurrence and likely remediation.",
                  [P("run_id", "Limit to one run")], ["run reconciliation"], ["rollback_run", "decision"], reconciliation_analysis),
        AgentSpec("performance-optimization", 10, "Performance Optimization", "Where observed run time goes (simulation only).",
                  [], ["runs"], ["decision"], performance_optimization),
        AgentSpec("compliance-verification", 11, "Compliance Verification", "14 control checks against platform state.",
                  [], ["audit", "projects", "runs", "delta", "tdm", "systems"], ["decision"], compliance_verification),
        AgentSpec("refresh-documentation", 12, "Refresh Documentation", "Generates markdown documentation from recorded facts.",
                  [P("kind", "project | postcopy_run | lean_build", True), P("id", "Record id", True)], ["records", "audit"], ["decision"], documentation),
    ]

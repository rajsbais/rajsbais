"""Explainable decision-support 'agents'.

These are deterministic, rule/score based advisors. There is NO LLM in the loop in the MVP. Every output lists its
inputs, rationale and `requires_human_approval`; none of them can execute or approve anything.
"""
from __future__ import annotations


def refresh_strategy(*, source_rows: int, scope_rows: int, source_gb: float, config_change_needed: bool,
                     freshness_days: int, masking_required: bool, target_free_gb: float | None = None,
                     cross_system_integrations: bool = False) -> dict:
    ratio = scope_rows / source_rows if source_rows else 1.0
    scores = {"selective": 50.0, "client_copy": 30.0, "full_system_refresh": 20.0}
    why: list[str] = []
    if ratio < 0.25:
        scores["selective"] += 30; why.append(f"scope is {ratio:.0%} of source rows: selective copy avoids moving the rest")
    elif ratio > 0.6:
        scores["full_system_refresh"] += 25; scores["selective"] -= 15
        why.append(f"scope is {ratio:.0%} of source rows: bulk copy is more efficient")
    if config_change_needed:
        scores["full_system_refresh"] += 35; scores["client_copy"] += 15; scores["selective"] -= 40
        why.append("customizing/repository must change: selective data copy never copies configuration")
    if freshness_days <= 7:
        scores["selective"] += 10; why.append("short freshness window favours repeatable incremental/selective refresh")
    if source_gb > 2000:
        scores["selective"] += 10; scores["full_system_refresh"] -= 5
        why.append("multi-TB source: full copies need storage/snapshot tooling and long downtime")
    if target_free_gb is not None and target_free_gb < source_gb:
        scores["full_system_refresh"] -= 40; why.append("target lacks capacity for a full copy")
        scores["selective"] += 10
    if masking_required:
        why.append("masking is mandatory for every option: full copy requires a post-copy masking phase and a release gate")
    if cross_system_integrations:
        scores["full_system_refresh"] -= 5; why.append("full copy requires re-pointing all integrations (post-copy factory)")
    best = max(scores, key=scores.get)
    return {"agent": "refresh-strategy", "recommendation": best, "scores": {k: round(v, 1) for k, v in scores.items()},
            "rationale": why, "inputs": {"scope_ratio": round(ratio, 3), "source_gb": source_gb},
            "method": "rule-based scoring (not a trained model)", "requires_human_approval": True}


def masking_recommendation(discovered: list[dict], policy_fields: set[tuple[str, str]], target_role: str) -> dict:
    missing = [d for d in discovered if (d["table"], d["field"]) not in policy_fields]
    recs = [{"table": d["table"], "field": d["field"], "strategy": d["strategy"], "category": d["category"],
             "reason": f"{d['source']} discovery, confidence {d['confidence']}"} for d in missing]
    mode = "ANONYMIZE" if target_role in ("SBX", "TRN", "UAT") else "PSEUDONYMIZE"
    return {"agent": "masking-recommendation", "add_rules": recs, "recommended_mode": mode,
            "rationale": [f"{len(recs)} sensitive fields are not covered by the active policy",
                          "ANONYMIZE (per-run key destroyed) suits widely accessible training/sandbox systems; "
                          "PSEUDONYMIZE keeps data stable across refreshes but remains re-identifiable with the key"],
            "requires_human_approval": True}


def conflict_resolution(report: dict, alternatives: list[dict]) -> dict:
    blocking = [f for f in report["findings"] if f["severity"] == "blocking"]
    best = max(alternatives, key=lambda a: (not a["blocking"], a["executable_objects"]), default=None)
    why = [f"{len(blocking)} blocking finding(s) in the current policy"]
    for a in alternatives:
        why.append(f"policy {a['policy']}: {'unblocked' if not a['blocking'] else 'still blocked'}, "
                   f"{a['executable_objects']} objects loadable, {a['quarantined']} quarantined")
    return {"agent": "target-conflict-analysis", "recommended_policy": best["policy"] if best else None,
            "alternatives": alternatives, "rationale": why,
            "note": "REPLACE is never recommended automatically; it needs an approved exception per object.",
            "requires_human_approval": True}


def estimate_duration(*, bytes_total: int, rows_total: int, extract_mb_s: float = 20.0, load_mb_s: float = 10.0,
                      mask_rows_s: float = 5000.0, overhead_s: float = 30.0) -> dict:
    mb = bytes_total / 1_048_576
    secs = overhead_s + mb / extract_mb_s + mb / load_mb_s + rows_total / mask_rows_s
    return {"seconds": round(secs, 1), "megabytes": round(mb, 3), "rows": rows_total,
            "model_assumption": True,
            "assumptions": {"extract_mb_s": extract_mb_s, "load_mb_s": load_mb_s, "mask_rows_s": mask_rows_s,
                            "overhead_s": overhead_s},
            "disclaimer": "Throughput figures are placeholders, not benchmark results. Calibrate against controlled tests."}

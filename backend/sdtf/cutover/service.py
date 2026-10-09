"""Cutover runbook generation with dependency-aware scheduling, critical path and downtime forecast.

Durations are template estimates scaled by manifest volumes and, where available, measured stage timings of
completed simulated runs. This is planning support, not a measured near-zero-downtime claim.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MigrationRun, ScopeManifest

TEMPLATE = [
    # id, name, phase, depends_on, base_minutes, per_1k_objects_minutes, downtime, owner, irreversible
    ("T01", "Freeze change management / transport lock", "PREPARE", [], 30, 0, False, "Basis", False),
    ("T02", "Final business approvals (manifest, rules, dispositions)", "PREPARE", [], 60, 0, False, "Business", False),
    ("T03", "Snapshot source statistics and baseline reconciliation", "PREPARE", ["T01"], 45, 0.5, False, "Migration", False),
    ("T04", "Initial extraction / load (pre-downtime)", "INITIAL_LOAD", ["T02", "T03"], 120, 4.0, False, "Migration", False),
    ("T05", "Delta capture active, backlog monitoring", "SYNC", ["T04"], 60, 0.2, False, "Migration", False),
    ("T06", "Business freeze: stop interfaces and batch jobs", "DOWNTIME", ["T05"], 30, 0, True, "Business/Basis", False),
    ("T07", "Final delta synchronisation", "DOWNTIME", ["T06"], 30, 0.8, True, "Migration", False),
    ("T08", "Final reconciliation (technical, functional, financial)", "DOWNTIME", ["T07"], 60, 0.6, True, "Migration/Finance", False),
    ("T09", "Go / no-go decision (technical + business sign-off)", "DOWNTIME", ["T08"], 30, 0, True, "Steering", False),
    ("T10", "Point of no return: enable target number ranges and interfaces", "DOWNTIME", ["T09"], 20, 0, True, "Basis", True),
    ("T11", "Residual data disposition in source (approved cleanup)", "POST", ["T10"], 90, 0.3, False, "Migration/Legal", True),
    ("T12", "Business validation and hypercare start", "POST", ["T10"], 120, 0, False, "Business", False),
    ("T13", "Production handover and evidence package sign-off", "POST", ["T11", "T12"], 60, 0, False, "PMO", False),
]


def generate_runbook(session: Session, m: ScopeManifest) -> dict:
    objects = m.impact.get("objects_total", 0)
    runs = session.execute(select(MigrationRun).where(MigrationRun.manifest_id == m.id, MigrationRun.status == "COMPLETED")).scalars().all()
    measured = {}
    for r in runs:
        for st in r.stages:
            measured[st.name] = max(measured.get(st.name, 0), st.duration_ms / 60000.0)
    tasks = []
    for tid, name, phase, deps, base, per1k, downtime, owner, irreversible in TEMPLATE:
        est = base + per1k * objects / 1000.0
        if tid == "T04" and measured:
            est = max(est, (measured.get("EXTRACT", 0) + measured.get("TRANSFORM", 0) + measured.get("LOAD", 0)) * 20)  # simulated timings scaled by a conservative factor
        tasks.append({"id": tid, "name": name, "phase": phase, "depends_on": deps, "est_minutes": round(est, 1), "downtime": downtime, "owner": owner, "irreversible": irreversible, "status": "PLANNED", "sign_off": "BUSINESS" if owner.startswith("Business") or owner == "Steering" else "TECHNICAL"})
    # critical path (longest path in DAG)
    finish: dict[str, float] = {}
    pred: dict[str, str | None] = {}
    for t in tasks:  # template is topologically ordered
        best, bp = 0.0, None
        for d in t["depends_on"]:
            if finish[d] > best:
                best, bp = finish[d], d
        t["earliest_start_min"] = round(best, 1)
        finish[t["id"]] = best + t["est_minutes"]
        t["earliest_finish_min"] = round(finish[t["id"]], 1)
        pred[t["id"]] = bp
    end = max(finish, key=finish.get)
    path = []
    cur: str | None = end
    while cur:
        path.append(cur)
        cur = pred[cur]
    path.reverse()
    downtime = sum(t["est_minutes"] for t in tasks if t["downtime"] and t["id"] in path)
    point_of_no_return = next((t["id"] for t in tasks if t["irreversible"]), None)
    return {
        "manifest_id": m.id,
        "tasks": tasks,
        "critical_path": path,
        "total_minutes": round(finish[end], 1),
        "forecast_downtime_minutes": round(downtime, 1),
        "point_of_no_return": point_of_no_return,
        "rollback": {"before_point_of_no_return": "Discard target load, unfreeze source, re-plan", "after_point_of_no_return": "Forward recovery only: fix in target, replay delta, re-reconcile", "irreversible_tasks": [t["id"] for t in tasks if t["irreversible"]]},
        "sign_offs": {"technical": [t["id"] for t in tasks if t["sign_off"] == "TECHNICAL"], "business": [t["id"] for t in tasks if t["sign_off"] == "BUSINESS"]},
        "basis": "Template durations scaled by scope volume; measured simulated stage timings are used with a conservative factor where available. Not a measured downtime guarantee.",
    }

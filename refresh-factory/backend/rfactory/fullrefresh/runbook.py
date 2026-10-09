"""Full system refresh: phase model, target guard and post-copy task catalog.

STATUS: design-level catalog + planning guard only. There is NO execution engine here: system copies must be driven by
SAP-supported tooling (SWPM, HANA backup/recovery, storage/VM snapshots) through adapters that are not implemented.
"""
from __future__ import annotations

from ..sap.adapter import SapSystem

PHASES = [
    (1, "Select source and target", "control-plane", False, "none"),
    (2, "Validate compatibility", "control-plane", False, "none"),
    (3, "Check target ownership and approvals", "control-plane", True, "target owner + change approver"),
    (4, "Confirm backup and recovery plan", "basis", True, "basis lead"),
    (5, "Capture target-specific configuration", "adapter", False, "re-import from captured export"),
    (6, "Quiesce target integrations and jobs", "adapter", True, "re-enable captured schedule"),
    (7, "Execute approved system-copy mechanism", "infrastructure adapter", True, "restore target backup/snapshot"),
    (8, "Perform SAP post-copy tasks", "post-copy factory", False, "per-task rollback"),
    (9, "Restore target-specific configuration", "adapter", False, "re-apply captured export"),
    (10, "Run smoke tests", "validation", False, "none"),
    (11, "Apply required masking", "masking engine", False, "restore from pre-mask snapshot"),
    (12, "Validate target security", "validation", True, "security officer"),
    (13, "Release the refreshed environment", "control-plane", True, "target owner"),
]

POST_COPY_TASKS = [
    {"id": "PC-001", "name": "Set logical system names (BDLS)", "area": "logical systems",
     "versions": ["ECC 6.0 EHP6+", "S/4HANA 1709+"], "prerequisites": ["target SID/logical system defined", "jobs stopped"],
     "precheck": "SCC4 logical system of target client matches target naming convention",
     "action": "Run BDLS conversion (old→new logical system) as a background job via released report",
     "postcheck": "No table rows reference the source logical system", "rollback": "Re-run BDLS in reverse mapping from backup",
     "evidence": "BDLS job log + residual-reference count", "approval": "basis lead"},
    {"id": "PC-002", "name": "Neutralize outbound RFC destinations", "area": "rfc",
     "versions": ["all"], "prerequisites": ["target RFC inventory captured"],
     "precheck": "SM59 destinations pointing at production hosts are listed",
     "action": "Re-point destinations to captured non-production targets; never copy production hosts",
     "postcheck": "No RFC destination resolves to a production host", "rollback": "Restore captured SM59 export",
     "evidence": "Before/after RFC destination diff", "approval": "integration owner"},
    {"id": "PC-003", "name": "Stop and reschedule background jobs", "area": "batch",
     "versions": ["all"], "prerequisites": ["job inventory"], "precheck": "Active/scheduled jobs inventoried",
     "action": "Cancel copied schedules, re-release only the target's approved job list",
     "postcheck": "Only whitelisted jobs are scheduled", "rollback": "Re-import captured SM37 schedule",
     "evidence": "Job list diff", "approval": "none"},
    {"id": "PC-004", "name": "Deactivate outbound email / SMTP routing", "area": "notifications",
     "versions": ["all"], "prerequisites": [], "precheck": "SCOT node points at production relay?",
     "action": "Point SMTP node at sandbox relay or sink", "postcheck": "Test mail lands in sink only",
     "rollback": "Restore SCOT export", "evidence": "SCOT config + test message id", "approval": "none"},
    {"id": "PC-005", "name": "Disable IDoc partner profiles to production partners", "area": "idoc",
     "versions": ["all"], "prerequisites": ["partner profile export"], "precheck": "WE20 outbound partners active",
     "action": "Set outbound partner status to inactive / re-point ports", "postcheck": "No outbound IDoc leaves target",
     "rollback": "Restore WE20/WE21 export", "evidence": "Partner profile diff", "approval": "integration owner"},
    {"id": "PC-006", "name": "Reconfigure TMS / transport routes", "area": "tms",
     "versions": ["all"], "prerequisites": ["STMS configuration exported"], "precheck": "Target in correct transport domain",
     "action": "Restore target transport configuration", "postcheck": "STMS consistency check green",
     "rollback": "Re-import STMS export", "evidence": "STMS check output", "approval": "basis lead"},
    {"id": "PC-007", "name": "Install SAP license", "area": "license", "versions": ["all"], "prerequisites": ["valid license key"],
     "precheck": "SLICENSE status", "action": "Install target license key", "postcheck": "License valid",
     "rollback": "n/a (temporary license reinstall)", "evidence": "License status", "approval": "none"},
    {"id": "PC-008", "name": "Reset SSO, trust relationships and certificates", "area": "security",
     "versions": ["all"], "prerequisites": ["STRUST export"], "precheck": "Production certificates present?",
     "action": "Remove production PSEs/trust, import target PSEs", "postcheck": "No production certificate remains",
     "rollback": "Restore STRUST export", "evidence": "STRUST inventory", "approval": "security officer"},
    {"id": "PC-009", "name": "Lock/reset privileged and dialog users per policy", "area": "security",
     "versions": ["all"], "prerequisites": ["target user policy"], "precheck": "Users from production present",
     "action": "Apply policy: lock non-whitelisted users, reset passwords, remove SAP_ALL", "postcheck": "No unexpected SAP_ALL",
     "rollback": "Restore USR02 snapshot", "evidence": "User policy report", "approval": "security officer"},
    {"id": "PC-010", "name": "Cloud destinations and Integration Suite endpoints", "area": "cloud",
     "versions": ["all"], "prerequisites": ["destination inventory"], "precheck": "Production tenant URLs present?",
     "action": "Re-point to non-production tenant", "postcheck": "No production tenant URL remains",
     "rollback": "Restore destination export", "evidence": "Destination diff", "approval": "integration owner"},
    {"id": "PC-011", "name": "HANA technical checks", "area": "hana", "versions": ["HANA 2.0"],
     "prerequisites": [], "precheck": "Backup catalog, log mode, license", "action": "Verify; no changes to HANA internals",
     "postcheck": "Checks green", "rollback": "n/a", "evidence": "Check output", "approval": "none"},
]
for _t in POST_COPY_TASKS:
    _t["status"] = "catalogued (not executable in MVP)"
    _t["never_reactivate_production_interfaces"] = True


def validate_pair(source: SapSystem, target: SapSystem) -> dict:
    blockers, warnings = [], []
    if target.is_production:
        blockers.append("Target is a production system: production systems can never be refresh targets")
    elif not target.writable_target_allowed:
        blockers.append("Target is locked against being overwritten (owner flag)")
    if source.id and source.id == target.id:
        blockers.append("Source and target are the same system")
    if source.sid == target.sid and source.client == target.client:
        blockers.append("Source and target share SID and client")
    if source.product != target.product:
        blockers.append(f"Product mismatch ({source.product} vs {target.product})")
    if source.db_type != target.db_type:
        blockers.append("Heterogeneous system copy (different DB type) is not supported by this orchestrator")
    if source.release != target.release:
        warnings.append("Different kernel/basis release: verify against SAP system copy guide")
    if not target.owner:
        warnings.append("Target has no registered owner: approval routing will fail")
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings}


def plan_full_refresh(source: SapSystem, target: SapSystem) -> dict:
    v = validate_pair(source, target)
    return {"validation": v, "executable": False,
            "note": "Execution engine not implemented; phases below are the approved runbook skeleton.",
            "phases": [{"no": n, "name": name, "performed_by": who, "approval_required": ap, "rollback": rb,
                        "status": "blocked" if (not v["ok"] and n >= 3) else "design-only"}
                       for n, name, who, ap, rb in PHASES]}

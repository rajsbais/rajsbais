"""Full system refresh: phase model, target guard and post-copy task catalog.

STATUS: the phase model and pair guard live here. The system copy itself (phase 7) has NO execution engine: it must be driven by
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

from ..postcopy.tasks import catalog as _catalog

POST_COPY_TASKS = _catalog()  # executable task library (simulated technical state): see rfactory/postcopy

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
        blockers.append(f"Product mismatch ({source.product} vs {target.product})"
                        + ("; ECC↔S/4HANA is a conversion/migration, outside refresh scope" if source.family != target.family else ""))
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
                        "status": "blocked" if (not v["ok"] and n >= 3) else ("executable-simulated (post-copy factory)" if n in (5, 8, 9, 12) else "design-only")}
                       for n, name, who, ap, rb in PHASES]}

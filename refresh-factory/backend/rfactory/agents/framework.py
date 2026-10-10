"""Agent framework: evidence-grounded recommendations with a governed, human-only path to action.

Design rules (enforced here, not by convention):
  * no evidence, no recommendation: a Recommendation cannot be created without rationale and evidence
  * an agent never executes anything; it proposes one action from a FIXED table whose flags (executable / destructive /
    production_access) are derived from the action kind, so an agent cannot label a dangerous action as harmless
  * applying a proposal is a human act done with the human's own permissions; agent principals can never accept, apply or reject
  * destructive and production-access proposals are hand-off only: the platform returns the exact call a human approver must make
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# kind -> flags and the permission a human needs to apply (or, for hand-off, to perform) it
ACTIONS: dict[str, dict] = {
    "decision": {"executable": False, "destructive": False, "production_access": False, "perm": "plan:write",
                 "note": "A choice for a human; nothing to execute."},
    # executable by a human through `apply` (non-destructive, reversible by editing again)
    "lock_system": {"executable": True, "destructive": False, "production_access": False, "perm": "system:write"},
    "set_conflict_policy": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    "add_masking_rules": {"executable": True, "destructive": False, "production_access": False, "perm": "masking:write"},
    "set_scope": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    "capture_profile": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    "create_postcopy_run": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    "adjust_delta_schedule": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    "adjust_delta_sweep": {"executable": True, "destructive": False, "production_access": False, "perm": "plan:write"},
    # hand-off only: never executed by an agent or by `apply`
    "rollback_run": {"executable": False, "destructive": True, "production_access": False, "perm": "run:execute"},
    "replace_objects": {"executable": False, "destructive": True, "production_access": False, "perm": "exception:approve"},
    "purge_dataset": {"executable": False, "destructive": True, "production_access": False, "perm": "plan:approve"},
    "decommission_client": {"executable": False, "destructive": True, "production_access": False, "perm": "plan:approve"},
    "unlock_system": {"executable": False, "destructive": False, "production_access": True, "perm": "plan:approve"},
    "change_source_access": {"executable": False, "destructive": False, "production_access": True, "perm": "plan:approve"},
}
PRIORITIES = ("critical", "high", "medium", "low", "info")
METHOD = "Deterministic analysis of platform metadata and execution evidence. No LLM makes decisions; an optional LLM may only narrate."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ev(source: str, ref: str, fact: str) -> dict:
    """One piece of evidence: where it comes from, what it refers to, and the fact itself."""
    return {"source": source, "ref": ref, "fact": fact}


@dataclass
class Recommendation:
    agent_id: str
    title: str
    summary: str
    priority: str
    confidence: str  # high | medium | low
    confidence_basis: str
    rationale: list[str]
    evidence: list[dict]
    action_kind: str = "decision"
    action_params: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"rec-{uuid.uuid4().hex[:8]}")
    report_id: str | None = None
    status: str = "PROPOSED"  # PROPOSED ACCEPTED REJECTED APPLIED HANDED_OFF
    created: str = field(default_factory=_now)
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None
    result: dict | None = None

    def __post_init__(self):
        if not self.evidence or not self.rationale:
            raise ValueError("no evidence, no recommendation: a recommendation needs rationale and evidence")
        if self.priority not in PRIORITIES or self.confidence not in ("high", "medium", "low"):
            raise ValueError("invalid priority or confidence")
        if self.action_kind not in ACTIONS:
            raise ValueError(f"unknown action kind {self.action_kind}")

    @property
    def action(self) -> dict:
        a = ACTIONS[self.action_kind]
        return {"kind": self.action_kind, "params": self.action_params, "executable": a["executable"], "destructive": a["destructive"],
                "production_access": a["production_access"], "requires_permission": a["perm"],
                "requires_approver": a["destructive"] or a["production_access"]}

    def public(self) -> dict:
        return {"id": self.id, "agent_id": self.agent_id, "report_id": self.report_id, "title": self.title, "summary": self.summary,
                "priority": self.priority, "confidence": self.confidence, "confidence_basis": self.confidence_basis, "rationale": self.rationale,
                "evidence": self.evidence, "action": self.action, "status": self.status, "created": self.created,
                "decided_by": self.decided_by, "decided_at": self.decided_at, "note": self.note, "result": self.result}


@dataclass
class AgentReport:
    agent_id: str
    name: str
    ran_by: str
    params: dict
    subject: str = ""
    summary: str = ""
    findings: list[dict] = field(default_factory=list)  # observations that need no action
    recommendations: list[Recommendation] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    narrative: dict | None = None
    id: str = field(default_factory=lambda: f"rpt-{uuid.uuid4().hex[:8]}")
    ran_at: str = field(default_factory=_now)

    # builders used by the agents
    def finding(self, level: str, text: str, evidence: list[dict] | None = None) -> None:
        self.findings.append({"level": level, "text": text, "evidence": evidence or []})

    def recommend(self, title: str, summary: str, priority: str, confidence: str, basis: str, rationale: list[str],
                  evidence: list[dict], kind: str = "decision", params: dict | None = None) -> Recommendation:
        r = Recommendation(self.agent_id, title, summary, priority, confidence, basis, rationale, evidence, kind, params or {})
        r.report_id = self.id
        self.recommendations.append(r)
        return r

    def public(self) -> dict:
        order = {p: i for i, p in enumerate(PRIORITIES)}
        recs = sorted(self.recommendations, key=lambda r: order[r.priority])
        return {"id": self.id, "agent_id": self.agent_id, "name": self.name, "ran_by": self.ran_by, "ran_at": self.ran_at, "params": self.params,
                "subject": self.subject, "summary": self.summary, "method": METHOD, "findings": self.findings,
                "recommendations": [r.public() for r in recs], "artifacts": self.artifacts, "limitations": self.limitations,
                "narrative": self.narrative, "simulated": True}


@dataclass
class AgentSpec:
    id: str
    number: int
    name: str
    description: str
    params: list[dict]
    reads: list[str]
    may_propose: list[str]
    fn: object = None

    def public(self) -> dict:
        return {"id": self.id, "number": self.number, "name": self.name, "description": self.description, "params": self.params,
                "reads": self.reads, "may_propose": [{"kind": k, **{x: ACTIONS[k][x] for x in ("executable", "destructive", "production_access")}} for k in self.may_propose]}

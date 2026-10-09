"""Bounded AI agent framework.

Agents operate on authorised metadata and evidence already stored in the platform. They *propose*; a human
with the right permission accepts or rejects. Every proposal carries a confidence score, evidence citations
(record ids / check names) and is persisted as an AgentDecision. Reasoning is pluggable: the default
HeuristicReasoner is deterministic; an LLM-backed reasoner is a planned extension behind the same interface
and would receive only the evidence bundle, never raw SAP credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from ..models import AgentDecision


@dataclass
class Evidence:
    kind: str  # SNAPSHOT | MANIFEST | RUN | RECONCILIATION | GRAPH | RULESET
    ref: str
    note: str = ""

    def as_dict(self):
        return {"kind": self.kind, "ref": self.ref, "note": self.note}


@dataclass
class Proposal:
    summary: str
    proposal: dict
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    requires_approval: bool = True
    subject_type: str = ""
    subject_id: str = ""


class Reasoner(Protocol):
    name: str

    def explain(self, facts: dict) -> str: ...


class HeuristicReasoner:
    name = "heuristic"
    status = "IMPLEMENTED"

    def explain(self, facts: dict) -> str:
        return "; ".join(f"{k}={v}" for k, v in facts.items())


class LlmReasoner:
    """Planned: LLM-backed explanation over the evidence bundle with the same contract."""

    name = "llm"
    status = "PLANNED"

    def explain(self, facts: dict) -> str:
        raise NotImplementedError("LLM reasoner is planned; the platform ships with the deterministic heuristic reasoner")


class Agent:
    name = "agent"
    description = ""
    permission = "agent:run"
    forbidden_actions = ("authorize_production_migration", "delete_data", "post_financial_adjustment", "change_security_policy")

    def __init__(self, reasoner: Reasoner | None = None):
        self.reasoner = reasoner or HeuristicReasoner()

    def propose(self, session: Session, project_id: str, context: dict) -> Proposal:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, session: Session, project_id: str, context: dict, actor: str) -> AgentDecision:
        p = self.propose(session, project_id, context)
        d = AgentDecision(project_id=project_id, agent=self.name, subject_type=p.subject_type, subject_id=p.subject_id, proposal={"summary": p.summary, **p.proposal, "requires_approval": p.requires_approval, "forbidden_actions": list(self.forbidden_actions)}, confidence=round(max(0.0, min(1.0, p.confidence)), 3), evidence=[e.as_dict() for e in p.evidence], status="PROPOSED", requested_by=actor)
        session.add(d)
        session.flush()
        return d


REGISTRY: dict[str, type[Agent]] = {}


def register(cls: type[Agent]) -> type[Agent]:
    REGISTRY[cls.name] = cls
    return cls


def decide(session: Session, decision: AgentDecision, accept: bool, actor: str) -> AgentDecision:
    if decision.status != "PROPOSED":
        raise ValueError(f"decision is already {decision.status}")
    decision.status = "ACCEPTED" if accept else "REJECTED"
    decision.decided_by = actor
    session.flush()
    return decision

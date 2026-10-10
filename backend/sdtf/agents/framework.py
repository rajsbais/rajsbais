"""Bounded AI agent framework.

Agents operate on authorised metadata and evidence already stored in the platform. They *propose*; a human
with the right permission accepts or rejects. Every proposal carries a confidence score, evidence citations
(record ids / check names) and is persisted as an AgentDecision. Reasoning is pluggable: the default
HeuristicReasoner is deterministic; the LLM-backed reasoner (`sdtf.agents.llm`, ADR-0018) sits behind the same
interface, receives only the redacted evidence bundle, never raw SAP credentials, and falls back to the heuristic
text whenever it is not configured, fails, or answers something the guard rejects.
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
        from .llm import heuristic_text

        return heuristic_text(facts)


def default_reasoner(agent_name: str = "") -> Reasoner:
    """The reasoner the configuration asks for: the LLM reasoner when a provider is set, else the heuristic one."""
    from ..config import settings

    if settings.llm_provider and settings.llm_provider != "none":
        from .llm import LlmReasoner

        return LlmReasoner(agent_name=agent_name)
    return HeuristicReasoner()


class Agent:
    name = "agent"
    description = ""
    permission = "agent:run"
    forbidden_actions = ("authorize_production_migration", "delete_data", "post_financial_adjustment", "change_security_policy")

    def __init__(self, reasoner: Reasoner | None = None):
        self.reasoner = reasoner or default_reasoner(self.name)
        if hasattr(self.reasoner, "agent_name") and not getattr(self.reasoner, "agent_name", ""):
            self.reasoner.agent_name = self.name

    def propose(self, session: Session, project_id: str, context: dict) -> Proposal:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, session: Session, project_id: str, context: dict, actor: str) -> AgentDecision:
        p = self.propose(session, project_id, context)
        last = getattr(self.reasoner, "last", None)
        reasoning = {"reasoner": self.reasoner.name, **(last.as_dict() if last is not None else {"status": "HEURISTIC"})}
        d = AgentDecision(project_id=project_id, agent=self.name, subject_type=p.subject_type, subject_id=p.subject_id, proposal={"summary": p.summary, **p.proposal, "reasoning": reasoning, "requires_approval": p.requires_approval, "forbidden_actions": list(self.forbidden_actions)}, confidence=round(max(0.0, min(1.0, p.confidence)), 3), evidence=[e.as_dict() for e in p.evidence], status="PROPOSED", requested_by=actor)
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

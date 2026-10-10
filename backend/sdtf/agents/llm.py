"""LLM-backed reasoner behind the `Reasoner` interface, and the prompt / evaluation harness around it.

The reasoner receives the evidence bundle only: a dictionary of facts the agent already computed from stored
evidence. Before anything leaves the process the bundle is redacted (keys that look like secrets are dropped,
long strings cut), and the answer is guarded: an explanation that mentions a number absent from the facts is
rejected and the deterministic heuristic explanation is used instead, with the reason recorded on the proposal.
The provider is configured by environment (`SDTF_LLM_PROVIDER`, `SDTF_LLM_MODEL`, the key in the variable named
by `SDTF_LLM_API_KEY_ENV`); without one the platform runs the heuristic reasoner. Nothing here reads an SAP
system or holds a credential of one: the agents never had such a path and the reasoner adds none.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import httpx

from ..config import settings

PROVIDERS = ("none", "anthropic", "openai")
DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
SECRET_KEY = re.compile(r"(passw|secret|token|api[_-]?key|credential|authorization|private)", re.IGNORECASE)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
MAX_STRING = 400
MAX_BUNDLE = 20_000
SYSTEM_PROMPT = (
    "You explain evidence gathered by an SAP selective data transformation platform to a migration architect. "
    "You are given a JSON object of facts the platform computed from its own stored evidence. Write two to four plain sentences "
    "that explain what the facts mean for the decision at hand. Use only the facts given: do not invent numbers, systems, "
    "tables or outcomes, and do not recommend actions the facts do not support. If the facts are insufficient, say so."
)


def redact(facts: dict, _depth: int = 0) -> dict:
    """The bundle that may leave the process: secret-looking keys dropped, long strings cut, depth bounded."""
    out: dict = {}
    for k, v in (facts or {}).items():
        if SECRET_KEY.search(str(k)):
            continue
        if isinstance(v, dict):
            out[k] = redact(v, _depth + 1) if _depth < 4 else "{…}"
        elif isinstance(v, (list, tuple)):
            out[k] = [redact(x, _depth + 1) if isinstance(x, dict) else (x[:MAX_STRING] if isinstance(x, str) else x) for x in list(v)[:50]]
        elif isinstance(v, str):
            out[k] = v[:MAX_STRING]
        else:
            out[k] = v
    return out


def build_messages(agent_name: str, facts: dict) -> tuple[str, str]:
    """(system prompt, user message) for a bundle; the bundle is redacted and size-bounded here, once."""
    bundle = json.dumps(redact(facts), default=str, sort_keys=True)
    if len(bundle) > MAX_BUNDLE:
        bundle = bundle[:MAX_BUNDLE] + "…"
    return SYSTEM_PROMPT, f"Agent: {agent_name or 'unknown'}\nFacts (JSON):\n{bundle}\n\nExplain these facts."


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text or "")}


def guard(text: str, facts: dict) -> str | None:
    """Why an answer is rejected, or None when it passes: empty, too long, or numbers that are not in the facts."""
    if not text or not text.strip():
        return "empty answer"
    if len(text) > 2000:
        return "answer too long"
    allowed = _numbers(json.dumps(facts, default=str))
    for a in list(allowed):  # a share of 0.71 may be said as 71 %
        try:
            v = float(a)
        except ValueError:
            continue
        if 0 < v <= 1:
            allowed |= {str(round(v * 100)), f"{v * 100:.1f}"}
    norm = {a.rstrip("0").rstrip(".") if "." in a else a for a in allowed}
    foreign = sorted(n for n in _numbers(text) if n not in allowed and (n.rstrip("0").rstrip(".") if "." in n else n) not in norm)
    if foreign:
        return f"mentions values not in the evidence: {', '.join(foreign[:5])}"
    return None


@dataclass
class ReasonerOutcome:
    status: str  # LLM | FALLBACK | HEURISTIC
    provider: str = "none"
    model: str = ""
    reason: str = ""
    prompt_chars: int = 0

    def as_dict(self):
        return {"status": self.status, "provider": self.provider, "model": self.model, "reason": self.reason, "prompt_chars": self.prompt_chars}


def heuristic_text(facts: dict) -> str:
    """The deterministic rendering (also redacted: no reasoner prints a secret-looking key)."""
    return "; ".join(f"{k}={v}" for k, v in redact(facts).items())


@dataclass
class LlmReasoner:
    """The `Reasoner` contract (`explain(facts) -> str`) over an HTTP chat endpoint, with fallback to the heuristic
    text whenever the provider is not configured, fails, or answers something the guard rejects."""

    provider: str = field(default_factory=lambda: settings.llm_provider)
    model: str = field(default_factory=lambda: settings.llm_model)
    base_url: str = field(default_factory=lambda: settings.llm_base_url)
    api_key_env: str = field(default_factory=lambda: settings.llm_api_key_env)
    timeout: float = field(default_factory=lambda: settings.llm_timeout_seconds)
    max_tokens: int = field(default_factory=lambda: settings.llm_max_tokens)
    client: httpx.Client | None = None
    agent_name: str = ""
    name: str = "llm"
    status: str = "IMPLEMENTED"
    last: ReasonerOutcome = field(default_factory=lambda: ReasonerOutcome("HEURISTIC"))

    # ---- configuration
    def configured(self) -> tuple[bool, str]:
        if self.provider not in PROVIDERS or self.provider == "none":
            return False, "no LLM provider configured (SDTF_LLM_PROVIDER)"
        if not self.model:
            return False, "no model configured (SDTF_LLM_MODEL)"
        if not os.getenv(self.api_key_env, ""):
            return False, f"no API key in the environment variable {self.api_key_env}"
        return True, ""

    def describe(self) -> dict:
        ok, why = self.configured()
        return {"name": self.name, "provider": self.provider, "model": self.model or None, "base_url": self.base_url or DEFAULT_BASE.get(self.provider), "api_key_env": self.api_key_env, "configured": ok, "note": why or "evidence bundle only, redacted; answers guarded against values absent from the facts; fallback to the heuristic text", "verified": "HTTP contract verified with a mocked transport in the test suite; not exercised against a live provider in this build"}

    # ---- the contract
    def explain(self, facts: dict) -> str:
        ok, why = self.configured()
        if not ok:
            self.last = ReasonerOutcome("FALLBACK", self.provider, self.model, why)
            return heuristic_text(facts)
        system, user = build_messages(self.agent_name, facts)
        try:
            text = self._call(system, user)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.last = ReasonerOutcome("FALLBACK", self.provider, self.model, f"{type(e).__name__}: {str(e)[:200]}", len(system) + len(user))
            return heuristic_text(facts)
        rejected = guard(text, facts)
        if rejected:
            self.last = ReasonerOutcome("FALLBACK", self.provider, self.model, f"answer rejected: {rejected}", len(system) + len(user))
            return heuristic_text(facts)
        self.last = ReasonerOutcome("LLM", self.provider, self.model, "", len(system) + len(user))
        return text.strip()

    # ---- providers
    def _call(self, system: str, user: str) -> str:
        key = os.environ[self.api_key_env]
        base = (self.base_url or DEFAULT_BASE[self.provider]).rstrip("/")
        client = self.client or httpx.Client(timeout=self.timeout)
        try:
            if self.provider == "anthropic":
                r = client.post(f"{base}/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json={"model": self.model, "max_tokens": self.max_tokens, "system": system, "messages": [{"role": "user", "content": user}]})
                r.raise_for_status()
                blocks = r.json()["content"]
                return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            r = client.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {key}", "content-type": "application/json"}, json={"model": self.model, "max_tokens": self.max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        finally:
            if self.client is None:
                client.close()


def reasoner_status() -> dict:
    """What the platform's agents reason with right now."""
    if settings.llm_provider == "none" or not settings.llm_provider:
        return {"name": "heuristic", "provider": "none", "model": None, "configured": True, "note": "deterministic heuristic reasoner (facts rendered as key=value); set SDTF_LLM_PROVIDER / SDTF_LLM_MODEL and the API key variable for the LLM reasoner", "verified": "deterministic"}
    return LlmReasoner().describe()


# ---------------------------------------------------------------------------------------------- evaluation
EVAL_CASES = [
    {"id": "scope-recent", "agent": "scope_recommendation", "facts": {"objects_total": 12480, "recent_share": 0.71, "shared_master_data": 37}, "must_mention": ["12480", "0.71", "37"]},
    {"id": "scope-small", "agent": "scope_recommendation", "facts": {"objects_total": 420, "recent_share": 0.2, "shared_master_data": 0}, "must_mention": ["420"]},
    {"id": "cutover-high", "agent": "cutover_risk", "facts": {"score": 82, "band": "HIGH", "open_documents": 310, "pending_approvals": 4, "interfaces": 6, "reconciliation": "WARN", "rehearsals_go": 0}, "must_mention": ["82", "310", "HIGH"]},
    {"id": "cutover-low", "agent": "cutover_risk", "facts": {"score": 12, "band": "LOW", "open_documents": 20, "pending_approvals": 0, "interfaces": 1, "reconciliation": "PASS", "rehearsals_go": 1}, "must_mention": ["12", "LOW"]},
    {"id": "recon-explained", "agent": "reconciliation_explanation", "facts": {"non_passing_checks": 3, "categories": {"SCOPE_POLICY": 2, "LOAD": 1}, "unknown": 0}, "must_mention": ["3"]},
    {"id": "secret-redacted", "agent": "cutover_risk", "facts": {"score": 40, "band": "MEDIUM", "api_key": "must-not-leak", "rfc_password": "must-not-leak"}, "must_mention": ["40"], "must_not_mention": ["must-not-leak"]},
]


def evaluate(reasoner, cases: list[dict] | None = None) -> dict:
    """Run the cases through a reasoner: every case must mention its key facts, mention nothing that was redacted,
    and pass the guard (no foreign numbers). The heuristic reasoner passes by construction; an LLM is measured."""
    results = []
    for c in cases or EVAL_CASES:
        if hasattr(reasoner, "agent_name"):
            reasoner.agent_name = c["agent"]
        text = reasoner.explain(c["facts"])
        outcome = getattr(reasoner, "last", None)
        missing = [m for m in c.get("must_mention", []) if m not in text]
        leaked = [m for m in c.get("must_not_mention", []) if m in text]
        rejected = guard(text, redact(c["facts"]))
        ok = not missing and not leaked and rejected is None
        results.append({"id": c["id"], "agent": c["agent"], "ok": ok, "missing": missing, "leaked": leaked, "guard": rejected, "status": outcome.status if outcome else "HEURISTIC", "reason": outcome.reason if outcome else "", "chars": len(text), "text": text[:300]})
    return {"reasoner": getattr(reasoner, "name", "?"), "cases": len(results), "passed": sum(1 for r in results if r["ok"]), "llm_answers": sum(1 for r in results if r["status"] == "LLM"), "results": results}


def eval_markdown(rep: dict) -> str:
    md = [f"# Reasoner evaluation: {rep['reasoner']}", "", f"{rep['passed']} of {rep['cases']} cases passed; {rep['llm_answers']} answered by the LLM, the rest by the heuristic fallback.", "", "| Case | Agent | OK | Answered by | Missing | Leaked | Guard | Reason |", "|---|---|---|---|---|---|---|---|"]
    for r in rep["results"]:
        md.append(f"| {r['id']} | {r['agent']} | {'yes' if r['ok'] else 'no'} | {r['status']} | {', '.join(r['missing']) or ''} | {', '.join(r['leaked']) or ''} | {r['guard'] or ''} | {r['reason'].replace('|', '/')} |")
    md += ["", "> Cases are synthetic fact bundles; a pass means the explanation cites the key facts, leaks nothing redacted and mentions no number absent from the evidence. It is not a measure of the advice's quality."]
    return "\n".join(md) + "\n"


__all__ = ["LlmReasoner", "ReasonerOutcome", "redact", "build_messages", "guard", "heuristic_text", "reasoner_status", "EVAL_CASES", "evaluate", "eval_markdown"]

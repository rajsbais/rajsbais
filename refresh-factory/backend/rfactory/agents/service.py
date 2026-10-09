"""AgentService: runs the agents, stores reports, and is the ONLY path from a recommendation to an action.

Guarantees (each covered by tests):
  * agents are read-only: running one never changes platform state (apart from the stored report and its audit entry)
  * accept / reject / apply are human acts using the human's own permissions; agent and service principals are refused
  * only `executable` action kinds can be applied; destructive and production-access kinds are hand-off only
  * a narrator (optional LLM) can only attach text to a report; it cannot alter recommendations
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from ..security.auth import Forbidden, Principal
from ..selective.manifest import Scope
from .framework import ACTIONS, AgentReport, AgentSpec, Recommendation
from .refresh_agents import specs

NARRATION_DISCLAIMER = ("Optional model-written summary of the structured findings above. It has no authority: it did not make, change or "
                        "rank any recommendation and may contain errors. The findings and evidence are the record.")
NARRATION_SYSTEM = ("You summarise structured findings from an SAP test-data refresh platform for a Basis administrator. "
                    "Use ONLY the facts provided. Do not add recommendations, do not invent numbers, do not mention anything not in the input. "
                    "Write at most 120 words of plain prose.")


class AgentError(RuntimeError):
    pass


class AnthropicNarrator:
    """Narrates a report using the Anthropic API. Only constructed when explicitly enabled; never required.

    Sends ONLY the structured text of findings/recommendations (titles, summaries, evidence facts) - no table rows, no
    masked or unmasked business data. Not exercised against the live API in this repository's tests (a fake is injected).
    """
    model = "claude-opus-5-5"

    def __init__(self, model: str | None = None):
        import anthropic  # optional dependency
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("RFACTORY_NARRATOR_MODEL", self.model)

    def __call__(self, system: str, user: str) -> str:
        msg = self.client.messages.create(model=self.model, max_tokens=600, system=system, messages=[{"role": "user", "content": user}])
        if msg.stop_reason == "refusal":
            raise AgentError("the model declined to narrate")
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def default_narrator():
    if os.environ.get("RFACTORY_NARRATOR") == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicNarrator()
        except Exception:  # missing SDK: narration is optional
            return None
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentService:
    def __init__(self, svc, narrator=None):
        self.svc = svc
        self.narrator = narrator if narrator is not None else default_narrator()
        self.specs: dict[str, AgentSpec] = {s.id: s for s in specs()}
        self.reports: dict[str, AgentReport] = {}
        self.recs: dict[str, Recommendation] = {}

    # ------------------------------------------------------------ catalogue / run
    def catalogue(self) -> dict:
        return {"agents": [s.public() for s in sorted(self.specs.values(), key=lambda s: s.number)],
                "narration_available": self.narrator is not None,
                "guarantees": ["Agents only read; they cannot change platform state, approve or execute.",
                               "Every recommendation carries evidence; without evidence it cannot be created.",
                               "Applying a recommendation is a human act with the human's own permissions.",
                               "Destructive and production-access actions are hand-off only.",
                               "No LLM makes decisions; narration is optional and advisory."]}

    def run(self, actor: Principal, agent_id: str, params: dict | None = None, narrate: bool = False) -> dict:
        if not actor.can("view"):
            raise Forbidden("view permission required")
        spec = self.specs.get(agent_id)
        if spec is None:
            raise AgentError(f"unknown agent {agent_id}")
        params = dict(params or {})
        from ..security import authz
        if authz.restricted(actor):  # an agent report must not become a way to read what the person may not open
            pid = params.get("project_id")
            if pid in self.svc.projects:
                authz.require_systems(self.svc, actor, self.svc.projects[pid].source_id, self.svc.projects[pid].target_id)
            authz.require_systems(self.svc, actor, params.get("system_id"))
            if spec.id in ("landscape-discovery", "compliance-verification", "refresh-scheduling", "performance-optimization", "reconciliation-analysis", "refresh-documentation") \
                    or not (params.get("project_id") or params.get("system_id")):
                raise Forbidden(f"the {spec.name} agent reads the whole platform: it is not available inside a scoped role")
        rep = AgentReport(spec.id, spec.name, actor.id, params)
        spec.fn(self.svc, params, rep)  # ValueError for bad input propagates
        for r in rep.recommendations:
            self.recs[r.id] = r
        if narrate:
            self._narrate(rep)
        self.reports[rep.id] = rep
        self.svc.audit.append(actor.id, "agent.run", rep.id, {"agent": spec.id, "recommendations": len(rep.recommendations), "narrated": bool(rep.narrative)})
        return rep.public()

    def _narrate(self, rep: AgentReport) -> None:
        if self.narrator is None:
            rep.limitations.append("Narration was requested but is not configured (set RFACTORY_NARRATOR=anthropic and ANTHROPIC_API_KEY).")
            return
        user = "\n".join([f"Agent: {rep.name}", f"Summary: {rep.summary}"] + [f"Finding ({f['level']}): {f['text']}" for f in rep.findings[:20]] +
                         [f"Recommendation ({r.priority}): {r.title} - {r.summary}" for r in rep.recommendations[:20]])
        try:
            text = self.narrator(NARRATION_SYSTEM, user)
        except Exception as e:  # narration must never break a report
            rep.limitations.append(f"Narration failed and was skipped: {type(e).__name__}")
            return
        rep.narrative = {"text": text, "model": getattr(self.narrator, "model", "custom"), "disclaimer": NARRATION_DISCLAIMER}

    def report(self, rid: str) -> dict:
        if rid not in self.reports:
            raise KeyError(rid)
        return self.reports[rid].public()

    def list_reports(self, agent_id: str | None = None) -> list[dict]:
        out = [r for r in self.reports.values() if not agent_id or r.agent_id == agent_id]
        return [{"id": r.id, "agent_id": r.agent_id, "name": r.name, "subject": r.subject, "summary": r.summary, "ran_by": r.ran_by,
                 "ran_at": r.ran_at, "recommendations": len(r.recommendations)} for r in reversed(out)]

    def list_recs(self, status: str | None = None, agent_id: str | None = None) -> list[dict]:
        return [r.public() for r in reversed(list(self.recs.values())) if (not status or r.status == status) and (not agent_id or r.agent_id == agent_id)]

    # ------------------------------------------------------------ human decisions
    def _rec(self, rid: str) -> Recommendation:
        if rid not in self.recs:
            raise KeyError(rid)
        return self.recs[rid]

    @staticmethod
    def _authorise(actor: Principal, rec: Recommendation) -> None:
        if actor.kind != "human":
            raise Forbidden(f"{actor.kind} principals cannot accept, reject or apply agent recommendations")
        need = rec.action["requires_permission"]
        if not actor.can(need):
            raise Forbidden(f"{need} required for this action")
        if rec.action["requires_approver"] and not actor.can("plan:approve"):
            raise Forbidden("destructive / production-access actions need an approver (plan:approve)")

    def _decide(self, actor: Principal, rid: str, status: str, note: str | None) -> Recommendation:
        rec = self._rec(rid)
        self._authorise(actor, rec)
        if rec.status not in ("PROPOSED", "ACCEPTED") or (status == "ACCEPTED" and rec.status != "PROPOSED"):
            raise AgentError(f"recommendation is {rec.status}")
        rec.status, rec.decided_by, rec.decided_at, rec.note = status, actor.id, _now(), note
        self.svc.audit.append(actor.id, f"agent.recommendation.{status.lower()}", rec.id, {"agent": rec.agent_id, "kind": rec.action_kind, "note": note})
        return rec

    def accept(self, actor: Principal, rid: str, note: str | None = None) -> dict:
        return self._decide(actor, rid, "ACCEPTED", note).public()

    def reject(self, actor: Principal, rid: str, note: str | None = None) -> dict:
        return self._decide(actor, rid, "REJECTED", note).public()

    def apply(self, actor: Principal, rid: str) -> dict:
        rec = self._rec(rid)
        self._authorise(actor, rec)
        if rec.status not in ("PROPOSED", "ACCEPTED"):
            raise AgentError(f"recommendation is {rec.status}")
        spec = ACTIONS[rec.action_kind]
        if not spec["executable"]:
            rec.result = {"handoff": self._handoff(rec), "executed": False,
                          "note": "This kind of action is never executed through an agent recommendation. Perform it with the call shown, as an authorised human."}
            rec.status = "HANDED_OFF"
        else:
            rec.result = {"executed": True, **self._execute(actor, rec)}
            rec.status = "APPLIED"
        rec.decided_by, rec.decided_at = actor.id, _now()
        self.svc.audit.append(actor.id, f"agent.recommendation.{rec.status.lower()}", rec.id,
                              {"agent": rec.agent_id, "kind": rec.action_kind, "params": rec.action_params, "executed": rec.result["executed"]})
        return rec.public()

    # ------------------------------------------------------------ execution (existing service methods, caller's own permissions)
    def _execute(self, actor: Principal, rec: Recommendation) -> dict:
        s, k, p = self.svc, rec.action_kind, rec.action_params
        if k == "lock_system":
            sysm = s.system(p["system_id"])
            sysm.writable_target_allowed = False
            s.audit.append(actor.id, "system.locked", sysm.id, {"via": rec.id})
            return {"system": sysm.label, "writable_target_allowed": False}
        if k == "set_conflict_policy":
            m = s.apply_conflict_policy(actor, p["project_id"], p["policy"])
            return {"manifest_version": m.version, "note": "the plan was reset: rebuild, re-analyze and re-approve"}
        if k == "add_masking_rules":
            st = s.add_masking_rules(actor, p["project_id"], p["rules"])
            return {"rules_added": len(p["rules"]), "uncovered": st["uncovered"], "note": "approval was reset"}
        if k == "set_scope":
            pr = s.project(p["project_id"])
            m = pr.manifest
            scope = Scope(**p["scope"]) if p.get("scope") else m.scope
            m2 = s.set_manifest(actor, pr.id, scope, p.get("include_downstream", m.include_downstream), m.masking_policy_id, m.conflict_policy,
                                m.instance_overrides)
            return {"manifest_version": m2.version, "note": "the plan was reset: rebuild, re-analyze and re-approve"}
        if k == "capture_profile":
            prof = s.postcopy.capture_profile(actor, p["system_id"], p["name"])
            return {"profile_id": prof.id, "status": prof.status, "note": "the profile still needs submit and approve"}
        if k == "create_postcopy_run":
            run = s.postcopy.create_run(actor, {"target_id": p["target_id"], "profile_id": p["profile_id"]})
            return {"run_id": run.id, "status": run.status, "approvals_needed": run.required_labels, "note": "created, not executed"}
        if k in ("adjust_delta_sweep", "adjust_delta_schedule"):
            spec = {"full_sweep_every": p["full_sweep_every"]} if k == "adjust_delta_sweep" else {"schedule": p["schedule"]}
            sc = s.delta.update(actor, p["scenario_id"], spec)
            return {"scenario": sc.id, "status": sc.status, "config_version": sc.config_version}
        raise AgentError(f"no executor for {k}")

    @staticmethod
    def _handoff(rec: Recommendation) -> dict:
        p = rec.action_params
        table = {
            "rollback_run": ("POST", f"/api/runs/{p.get('run_id')}/rollback"),
            "decommission_client": ("POST", f"/api/lean/builds/{p.get('build_id')}/decommission"),
            "purge_dataset": ("POST", f"/api/tdm/datasets/{p.get('dataset_id')}/purge"),
            "replace_objects": ("POST", f"/api/projects/{p.get('project_id')}/exceptions"),
            "unlock_system": ("PUT", f"/api/systems/{p.get('system_id')}"),
            "change_source_access": ("PUT", f"/api/systems/{p.get('system_id')}"),
        }
        m, path = table.get(rec.action_kind, ("-", "-"))
        return {"method": m, "path": path, "requires_permission": rec.action["requires_permission"], "params": p}

    # ------------------------------------------------------------ copilot: a keyword router, not an LLM
    ROUTES = [("complian", "compliance-verification", {}), ("audit", "compliance-verification", {}), ("landscape", "landscape-discovery", {}),
              ("system", "landscape-discovery", {}), ("strategy", "refresh-strategy", {"project_id"}), ("which refresh", "refresh-strategy", {"project_id"}),
              ("dependen", "business-dependency", {"project_id"}), ("scope", "scope-optimization", {"project_id"}), ("smaller", "scope-optimization", {"project_id"}),
              ("mask", "masking-recommendation", {"project_id"}), ("pii", "masking-recommendation", {"project_id"}),
              ("schedul", "refresh-scheduling", {}), ("delta", "refresh-scheduling", {}), ("conflict", "conflict-analysis", {"project_id"}),
              ("post-copy", "postcopy-automation", {"system_id"}), ("postcopy", "postcopy-automation", {"system_id"}),
              ("reconcil", "reconciliation-analysis", {}), ("held", "reconciliation-analysis", {}), ("slow", "performance-optimization", {}),
              ("perform", "performance-optimization", {}), ("document", "refresh-documentation", {"id", "kind"})]

    def copilot(self, actor: Principal, question: str, params: dict | None = None) -> dict:
        q, params = question.lower(), params or {}
        for kw, aid, need in self.ROUTES:
            if kw in q:
                missing = [n for n in need if not params.get(n)]
                if missing:
                    return {"routed_to": aid, "needs": sorted(missing), "answer": None,
                            "note": f"Routed to {self.specs[aid].name}; provide {', '.join(sorted(missing))} to run it.", "method": "keyword routing (not an LLM)"}
                return {"routed_to": aid, "report": self.run(actor, aid, params), "method": "keyword routing (not an LLM)"}
        return {"routed_to": None, "report": None, "method": "keyword routing (not an LLM)",
                "note": "I can run: " + ", ".join(s.name for s in self.specs.values()) + ". Mention one of these topics."}

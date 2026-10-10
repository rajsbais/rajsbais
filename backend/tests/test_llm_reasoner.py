"""LLM reasoner behind the Reasoner interface: redaction of the evidence bundle, the HTTP contract of both
providers verified with a mocked transport, the guard against foreign numbers, fallback to the heuristic text,
the outcome recorded on the proposal, the evaluation harness, API status and CLI."""
import json

import httpx

from sdtf.agents import llm
from sdtf.agents.catalog import agent_catalog  # noqa: F401  (registers the agents)
from sdtf.agents.framework import REGISTRY, HeuristicReasoner, default_reasoner
from sdtf.agents.llm import LlmReasoner, build_messages, evaluate, guard, redact

API = "/api/v1"
FACTS = {"objects_total": 12480, "recent_share": 0.71, "shared_master_data": 37, "nested": {"rfc_password": "x", "ok": "y"}, "api_key": "leak", "long": "a" * 1000}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _anthropic(text, status=200):
    def handler(req: httpx.Request):
        assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "k-test" and req.headers["anthropic-version"]
        body = json.loads(req.content)
        assert body["model"] == "m" and body["system"].startswith("You explain") and "leak" not in body["messages"][0]["content"] and "rfc_password" not in body["messages"][0]["content"]
        assert body["messages"][0]["content"].startswith("Agent: scope_recommendation")
        return httpx.Response(status, json={"content": [{"type": "text", "text": text}]} if status == 200 else {"error": "boom"})
    return handler


def test_redaction_and_messages():
    r = redact(FACTS)
    assert "api_key" not in r and "rfc_password" not in r["nested"] and r["nested"]["ok"] == "y" and len(r["long"]) == 400 and r["objects_total"] == 12480
    system, user = build_messages("scope_recommendation", FACTS)
    assert "leak" not in user and "Facts (JSON)" in user and "do not invent numbers" in system
    assert guard("Of 12,480 objects, 71 % are recent (0.71) and 37 are shared.", FACTS) is None
    assert guard("Of 12480 objects 9999 are old.", FACTS) == "mentions values not in the evidence: 9999"
    assert guard("", FACTS) == "empty answer" and guard("x" * 2001, FACTS) == "answer too long"


def test_llm_reasoner_anthropic_and_openai(monkeypatch):
    monkeypatch.setenv("SDTF_LLM_API_KEY", "k-test")
    r = LlmReasoner(provider="anthropic", model="m", client=_client(_anthropic("Of 12480 objects, a share of 0.71 is recent; 37 master records are shared.")), agent_name="scope_recommendation")
    assert r.configured() == (True, "")
    assert r.explain(FACTS).startswith("Of 12480 objects") and r.last.status == "LLM" and r.last.provider == "anthropic" and r.last.prompt_chars > 100

    def openai(req: httpx.Request):
        assert req.url.path == "/v1/chat/completions" and req.headers["authorization"] == "Bearer k-test" and req.url.host == "llm.local"
        body = json.loads(req.content)
        assert body["messages"][0]["role"] == "system" and body["messages"][1]["role"] == "user"
        return httpx.Response(200, json={"choices": [{"message": {"content": "The 12480 objects are mostly recent (0.71)."}}]})

    r = LlmReasoner(provider="openai", model="m", base_url="http://llm.local/", client=_client(openai), agent_name="scope_recommendation")
    assert r.explain(FACTS) == "The 12480 objects are mostly recent (0.71)." and r.last.status == "LLM"
    d = r.describe()
    assert d["configured"] and d["api_key_env"] == "SDTF_LLM_API_KEY" and "k-test" not in json.dumps(d) and d["base_url"] == "http://llm.local/"


def test_llm_reasoner_falls_back(monkeypatch):
    monkeypatch.delenv("SDTF_LLM_API_KEY", raising=False)
    r = LlmReasoner(provider="anthropic", model="m", agent_name="scope_recommendation")
    assert r.explain(FACTS) == llm.heuristic_text(FACTS) and r.last.status == "FALLBACK" and "SDTF_LLM_API_KEY" in r.last.reason
    assert LlmReasoner(provider="anthropic", model="", agent_name="x").configured()[1].startswith("no model")
    assert LlmReasoner(provider="none").configured()[1].startswith("no LLM provider")
    monkeypatch.setenv("SDTF_LLM_API_KEY", "k-test")
    r = LlmReasoner(provider="anthropic", model="m", client=_client(_anthropic("", status=500)), agent_name="scope_recommendation")
    assert r.explain(FACTS) == llm.heuristic_text(FACTS) and r.last.status == "FALLBACK" and r.last.reason.startswith("HTTPStatusError")
    r = LlmReasoner(provider="anthropic", model="m", client=_client(_anthropic("There are 99999 objects.")), agent_name="scope_recommendation")
    assert r.explain(FACTS) == llm.heuristic_text(FACTS) and r.last.status == "FALLBACK" and "99999" in r.last.reason

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    r = LlmReasoner(provider="anthropic", model="m", client=_client(timeout), agent_name="scope_recommendation")
    assert r.explain(FACTS) == llm.heuristic_text(FACTS) and r.last.reason.startswith("ReadTimeout")


def test_agents_record_the_reasoning(session, slice_result, monkeypatch):
    # default: heuristic
    d = REGISTRY["cutover_risk"]().run(session, slice_result["project_id"], {"manifest_id": slice_result["manifest_id"]}, "architect")
    assert d.proposal["reasoning"] == {"reasoner": "heuristic", "status": "HEURISTIC"} and d.proposal["narrative"].startswith("score=")
    assert isinstance(default_reasoner(), HeuristicReasoner)
    # an injected LLM reasoner: the narrative is the model's answer and the proposal says so
    monkeypatch.setenv("SDTF_LLM_API_KEY", "k-test")

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        facts = json.loads(body["messages"][0]["content"].split("Facts (JSON):\n", 1)[1].split("\n\nExplain", 1)[0])
        return httpx.Response(200, json={"content": [{"type": "text", "text": f"Risk score {facts['score']} is {facts['band']} with {facts['open_documents']} open documents."}]})

    agent = REGISTRY["cutover_risk"](reasoner=LlmReasoner(provider="anthropic", model="m", client=_client(handler)))
    assert agent.reasoner.agent_name == "cutover_risk"
    d = agent.run(session, slice_result["project_id"], {"manifest_id": slice_result["manifest_id"]}, "architect")
    assert d.proposal["reasoning"]["status"] == "LLM" and d.proposal["narrative"].startswith(f"Risk score {d.proposal['score']} is {d.proposal['band']}")
    d = REGISTRY["reconciliation_explanation"](reasoner=LlmReasoner(provider="anthropic", model="m", client=_client(lambda req: httpx.Response(200, json={"content": [{"type": "text", "text": "Nothing to explain: 0 checks failed."}]})))).run(session, slice_result["project_id"], {"run_id": slice_result["run_id"]}, "architect")
    assert d.proposal["facts"]["non_passing_checks"] == 0 and d.proposal["reasoning"]["status"] == "LLM" and d.proposal["narrative"].endswith("0 checks failed.")
    # configuration switch: a provider set in the settings makes default_reasoner return the LLM one (unconfigured key -> fallback)
    import dataclasses

    import sdtf.config as cfg

    patched = dataclasses.replace(cfg.settings, llm_provider="anthropic", llm_model="m")
    monkeypatch.setattr(cfg, "settings", patched)
    monkeypatch.setattr(llm, "settings", patched)
    monkeypatch.delenv("SDTF_LLM_API_KEY")
    r = default_reasoner("scope_recommendation")
    assert isinstance(r, LlmReasoner) and r.agent_name == "scope_recommendation"
    d = REGISTRY["scope_recommendation"]().run(session, slice_result["project_id"], {"company_code": "5000"}, "architect")
    assert d.proposal["reasoning"]["reasoner"] == "llm" and d.proposal["reasoning"]["status"] == "FALLBACK" and "SDTF_LLM_API_KEY" in d.proposal["reasoning"]["reason"]


def test_evaluation_harness(monkeypatch):
    rep = evaluate(HeuristicReasoner())
    assert rep["passed"] == rep["cases"] == len(llm.EVAL_CASES) and rep["llm_answers"] == 0 and all(r["status"] == "HEURISTIC" for r in rep["results"])
    monkeypatch.setenv("SDTF_LLM_API_KEY", "k-test")

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        user = body["messages"][0]["content"]
        facts = json.loads(user.split("Facts (JSON):\n", 1)[1].split("\n\nExplain", 1)[0])
        if facts.get("score") == 82:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "The score is 82 and 1234 documents are open."}]})  # foreign number -> fallback, still passes the harness
        if facts.get("objects_total") == 420:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "A small scope."}]})  # LLM answer without the key fact -> case fails
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Facts: " + ", ".join(f"{k} {v}" for k, v in facts.items() if not isinstance(v, dict))}]})

    rep = evaluate(LlmReasoner(provider="anthropic", model="m", client=_client(handler)))
    by = {r["id"]: r for r in rep["results"]}
    assert by["cutover-high"]["status"] == "FALLBACK" and by["cutover-high"]["ok"] and "1234" in by["cutover-high"]["reason"]
    assert by["scope-small"]["status"] == "LLM" and not by["scope-small"]["ok"] and by["scope-small"]["missing"] == ["420"]
    assert by["secret-redacted"]["ok"] and by["secret-redacted"]["status"] == "LLM" and not by["secret-redacted"]["leaked"]
    assert rep["passed"] == rep["cases"] - 1 and rep["llm_answers"] == rep["cases"] - 1
    md = llm.eval_markdown(rep)
    assert "| scope-small | scope_recommendation | no | LLM | 420 |" in md and "not a measure of the advice's quality" in md


def test_reasoner_api_and_cli(client, tokens, capsys, tmp_path):
    rs = client.get(f"{API}/agents/reasoner", headers=tokens["viewer"]).json()
    assert rs["name"] == "heuristic" and rs["configured"] and rs["provider"] == "none"
    assert all(a["reasoner"] == "heuristic (deterministic)" for a in client.get(f"{API}/agents", headers=tokens["viewer"]).json())
    from sdtf.cli import main

    assert main(["llm-eval"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Reasoner evaluation: heuristic") and f"{len(llm.EVAL_CASES)} of {len(llm.EVAL_CASES)} cases passed" in out
    assert main(["llm-eval", "--json", "--out", str(tmp_path / "eval.md")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("written ") and json.loads(out.split("\n", 1)[1])["configuration"]["name"] == "heuristic" and (tmp_path / "eval.md").exists()

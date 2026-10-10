# ADR-0018: LLM reasoner behind the Reasoner interface

**Status**: accepted (implemented; HTTP contract verified with a mocked transport, not against a live provider in
this build)

## Context

The twelve bounded agents (`docs/08-security-approval-model.md`) propose from stored evidence and explain their
facts through a `Reasoner` with one method, `explain(facts) -> str`. The platform shipped with the deterministic
`HeuristicReasoner` (facts rendered as `key=value`). An LLM-backed reasoner was planned behind the same interface
with two conditions: it receives the evidence bundle only, never an SAP credential, and the platform's behaviour
must not depend on it.

## Decision

`sdtf/agents/llm.py` implements `LlmReasoner` with the same contract:

* **Input**: the facts dictionary the agent computed. Before anything leaves the process the bundle is
  **redacted** (keys that look like secrets are dropped, strings cut at 400 characters, depth and size bounded)
  and rendered as JSON into a fixed prompt that asks for two to four sentences using only the facts given.
* **Providers**: the Anthropic Messages API and any OpenAI-compatible chat completions endpoint (a gateway or a
  local server through `SDTF_LLM_BASE_URL`), over `httpx`. The API key is read from the environment variable
  named by `SDTF_LLM_API_KEY_ENV` (default `SDTF_LLM_API_KEY`) at call time; it is never stored, logged or
  returned by the API. No model identifier is built in: `SDTF_LLM_MODEL` names the model the provider offers.
* **Guard**: an answer is rejected when it is empty, too long, or mentions a number that is not in the facts.
* **Fallback**: when no provider is configured, the call fails or times out, or the guard rejects the answer, the
  reasoner returns the heuristic text. The outcome (`LLM`, `FALLBACK` with the reason, or `HEURISTIC`) is
  recorded on every proposal under `reasoning`, so a reader always knows which text they are looking at.
* **Selection**: `default_reasoner()` returns the LLM reasoner when `SDTF_LLM_PROVIDER` is set, else the heuristic
  one; `GET /agents/reasoner` and the agent catalogue say which is in use and whether it is configured.
* **Evaluation harness**: `evaluate()` runs synthetic fact bundles through a reasoner and checks that each
  explanation cites the key facts, leaks nothing redacted and mentions no foreign number; `sdtf llm-eval` prints
  the report. It measures faithfulness to the evidence, not the quality of advice.

The agents that use the reasoner are the scope recommendation (summary), the cutover risk (a `narrative` next
to the score, band and factors) and the reconciliation explanation (a `narrative` next to the categorised
explanations). The deterministic parts of every proposal are unchanged; the reasoner only adds words.

## Consequences

* Agents remain bounded: proposals carry confidence, evidence citations and forbidden actions as before; the
  reasoner has no tool access, no session, no path to an SAP system, and cannot change a proposal's data.
* The platform runs identically without a provider; CI and the test suite exercise the HTTP contract with a
  mocked transport only. Nobody in this build has called a live provider: a first live run should start with
  `sdtf llm-eval` and read the report.
* Prompt text and the evaluation cases are code, versioned with the platform.

import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const PRI: Record<string, string> = { critical: "bad", high: "bad", medium: "warn", low: "info", info: "info" };
const NEEDS_PROJECT = ["project_id"];

function Rec({ r, onDone }: { r: J; onDone: () => void }) {
  const app = useApp();
  const act = useAction();
  const a = r.action;
  const open = r.status === "PROPOSED" || r.status === "ACCEPTED";
  const can = app.me?.kind === "human" && app.can(a.requires_permission) && (!a.requires_approver || app.can("plan:approve"));
  const go = (path: string, body?: unknown) => act.run(async () => { await api.post(`/api/agents/recommendations/${r.id}/${path}`, body ?? {}); app.reload(); onDone(); });
  return (
    <div className="rec" data-testid="rec">
      <div className="row"><Badge kind={PRI[r.priority]}>{r.priority}</Badge><strong>{r.title}</strong><StatusBadge s={r.status} />
        <Badge kind={a.executable ? "ok" : a.kind === "decision" ? "info" : "warn"}>{a.executable ? `can apply: ${a.kind}` : a.kind === "decision" ? "decision for a human" : `hand-off only: ${a.kind}`}</Badge></div>
      <p>{r.summary}</p>
      <p className="muted small">Confidence <strong>{r.confidence}</strong> — {r.confidence_basis}</p>
      <details><summary>Why, and the evidence ({r.evidence.length})</summary>
        <ul>{r.rationale.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>
        <table className=""><thead><tr><th>Source</th><th>Reference</th><th>Fact</th></tr></thead>
          <tbody>{r.evidence.map((e: J, i: number) => <tr key={i}><td>{e.source}</td><td className="mono">{e.ref}</td><td>{e.fact}</td></tr>)}</tbody></table>
        {Object.keys(a.params).length > 0 && <pre className="small">{JSON.stringify(a.params, null, 1)}</pre>}
      </details>
      {r.result?.handoff && <div className="callout"><strong>Not executed.</strong> {r.result.note}<pre className="small">{r.result.handoff.method} {r.result.handoff.path} (needs {r.result.handoff.requires_permission})</pre></div>}
      {r.result?.executed && <p className="small ok-text">Applied by {r.decided_by}: {r.result.note ?? "done"}</p>}
      {open && (
        <div className="row">
          <button className="primary" disabled={!can || act.busy} title={can ? "" : `needs ${a.requires_permission}${a.requires_approver ? " + plan:approve" : ""} as a human`} onClick={() => go("apply")}>{a.executable ? "Apply" : "Record hand-off"}</button>
          <button disabled={!can || act.busy} onClick={() => go("reject", { note: "rejected in UI" })}>Reject</button>
          {!can && <span className="muted small">You cannot decide this one{app.me?.kind !== "human" ? " (agents and services never can)" : ""}.</span>}
        </div>)}
      <ErrorNote error={act.error} />
    </div>
  );
}

export default function Agents() {
  const app = useApp();
  const [cat, setCat] = useState<J>(null);
  const [sel, setSel] = useState("landscape-discovery");
  const [params, setParams] = useState<Record<string, string>>({});
  const [rep, setRep] = useState<J>(null);
  const [recs, setRecs] = useState<J[]>([]);
  const [q, setQ] = useState(""); const [narrate, setNarrate] = useState(false);
  const act = useAction();
  const agent = cat?.agents.find((a: J) => a.id === sel);

  const loadRecs = useCallback(async (id?: string) => {
    const all: J[] = await api.get("/api/agents/recommendations");
    setRecs(all);
    if (id) setRep(await api.get(`/api/agents/reports/${id}`));
  }, []);
  useEffect(() => { act.run(async () => setCat(await api.get("/api/agents"))); }, [app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (app.project && !params.project_id) setParams((p) => ({ ...p, project_id: app.project!.id })); }, [app.project?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { const t = app.systems.find((s) => s.role !== "PRD"); if (t && !params.system_id) setParams((p) => ({ ...p, system_id: t.id })); }, [app.systems.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const body = () => Object.fromEntries((agent?.params ?? []).map((p: J) => [p.name, params[p.name]]).filter(([, v]: J) => v !== undefined && v !== ""));
  const run = () => act.run(async () => { const r = await api.post(`/api/agents/${sel}/run`, { params: body(), narrate }); setRep(r); await loadRecs(); });
  const ask = () => act.run(async () => {
    const r = await api.post("/api/agents/copilot", { question: q, params: { ...params } });
    if (r.report) { setSel(r.routed_to); setRep(r.report); await loadRecs(); } else setRep({ copilot: r });
  });
  if (!cat) return <Card title="AI refresh agents"><ErrorNote error={act.error} /></Card>;

  return (
    <>
      <Card title="AI refresh agents">
        <p className="muted">Twelve agents that <strong>read</strong> platform state and propose actions with evidence. They never approve, execute, or touch production; applying a proposal is a human act under your own permissions. Analysis is deterministic — <strong>no LLM makes any decision</strong>.
          {cat.narration_available ? " Optional model narration is enabled." : " Optional model narration is not configured."}</p>
        <div className="row"><input aria-label="Ask the copilot" placeholder="Ask: are we compliant? what masking do I need? …" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q && ask()} style={{ flex: 1 }} />
          <button disabled={!q || act.busy} onClick={ask}>Ask</button></div>
        <p className="muted small">The copilot is a keyword router to the agents below, not a chat model.</p>
      </Card>
      <div className="grid two">
        <Card title="Agents">
          <ul className="plain">{cat.agents.map((a: J) => (
            <li key={a.id}><button className={a.id === sel ? "primary" : ""} style={{ width: "100%", textAlign: "left" }} onClick={() => { setSel(a.id); setRep(null); }}>{a.number}. {a.name}</button></li>))}</ul>
        </Card>
        <Card title={agent ? `${agent.number}. ${agent.name}` : ""}>
          <p>{agent?.description}</p>
          <p className="muted small">Reads: {agent?.reads.join(", ")}. May propose: {agent?.may_propose.map((m: J) => m.kind).join(", ")}.</p>
          <div className="form">
            {agent?.params.map((p: J) => NEEDS_PROJECT.includes(p.name)
              ? <label key={p.name}>Project<select aria-label="Project" value={params.project_id ?? ""} onChange={(e) => setParams({ ...params, project_id: e.target.value })}><option value="">—</option>{app.projects.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></label>
              : p.name === "system_id"
                ? <label key={p.name}>System<select aria-label="System" value={params.system_id ?? ""} onChange={(e) => setParams({ ...params, system_id: e.target.value })}>{app.systems.filter((s) => s.role !== "PRD").map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
                : <label key={p.name}>{p.name}<input aria-label={p.name} placeholder={p.description} value={params[p.name] ?? ""} onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} /></label>)}
          </div>
          <div className="row"><button className="primary" disabled={act.busy} onClick={run}>Run agent</button>
            <label className="check"><input type="checkbox" checked={narrate} disabled={!cat.narration_available} onChange={(e) => setNarrate(e.target.checked)} />Add model narration (optional)</label></div>
          <ErrorNote error={act.error} />
        </Card>
      </div>
      {rep?.copilot && <Card title="Copilot"><p>{rep.copilot.note}</p><p className="muted small">{rep.copilot.method}</p></Card>}
      {rep?.id && (
        <Card title={`Report: ${rep.name}`}>
          <p><strong>{rep.summary}</strong></p>
          <p className="muted small">Subject: {rep.subject || "—"} · run by {rep.ran_by} · {rep.method}</p>
          {rep.narrative && <div className="callout"><strong>Model narration</strong> ({rep.narrative.model}): {rep.narrative.text}<p className="muted small">{rep.narrative.disclaimer}</p></div>}
          {rep.findings.length > 0 && <><h4>Findings</h4><ul>{rep.findings.map((f: J, i: number) => <li key={i}><Badge kind={PRI[f.level] ?? (f.level === "blocking" ? "bad" : f.level === "warning" ? "warn" : "info")}>{f.level}</Badge> {f.text}</li>)}</ul></>}
          {rep.artifacts.controls && <DataTable pageSize={20} rows={rep.artifacts.controls} cols={[{ key: "id", title: "Control" }, { key: "name", title: "Check" }, { key: "status", title: "Result", render: (c: J) => <StatusBadge s={c.status} /> }, { key: "detail", title: "Detail" }]} />}
          {rep.artifacts.markdown && <pre className="small" style={{ whiteSpace: "pre-wrap" }}>{rep.artifacts.markdown}</pre>}
          <h4>Recommendations ({rep.recommendations.length})</h4>
          {rep.recommendations.length === 0 && <p className="muted">Nothing to recommend — the agent found no evidence-backed reason to act.</p>}
          {rep.recommendations.map((r: J) => <Rec key={r.id} r={recs.find((x) => x.id === r.id) ?? r} onDone={() => loadRecs(rep.id)} />)}
          <h4>Limitations</h4><ul className="muted small">{rep.limitations.map((l: string, i: number) => <li key={i}>{l}</li>)}</ul>
        </Card>)}
    </>
  );
}

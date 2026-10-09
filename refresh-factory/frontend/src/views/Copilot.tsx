import { useState } from "react";
import { api, J } from "../api";
import { Bars, Badge, Card, ErrorNote, useAction } from "../components";
import { useApp } from "../ctx";

export default function Copilot() {
  const { project } = useApp();
  const [r, setR] = useState<J>(null);
  const [f, setF] = useState({ source_gb: 3000, config_change_needed: false, freshness_days: 7, masking_required: true, cross_system_integrations: false });
  const act = useAction();
  return (
    <>
      <Card title="Refresh strategy recommendation">
        <p className="muted">Decision support only. Rule-based scoring over your project scope — <strong>no LLM is involved</strong>, and the Copilot cannot approve or execute anything.</p>
        <div className="form">
          <label>Source size (GB)<input type="number" value={f.source_gb} onChange={(e) => setF({ ...f, source_gb: Number(e.target.value) })} /></label>
          <label>Required freshness (days)<input type="number" value={f.freshness_days} onChange={(e) => setF({ ...f, freshness_days: Number(e.target.value) })} /></label>
          <label className="check"><input type="checkbox" checked={f.config_change_needed} onChange={(e) => setF({ ...f, config_change_needed: e.target.checked })} />Customizing / repository must change</label>
          <label className="check"><input type="checkbox" checked={f.cross_system_integrations} onChange={(e) => setF({ ...f, cross_system_integrations: e.target.checked })} />Many connected systems</label>
        </div>
        <button className="primary" disabled={!project?.has_plan || act.busy} onClick={() => act.run(async () => setR(await api.post(`/api/projects/${project!.id}/agents/strategy`, f)))}>Recommend</button>
        {!project?.has_plan && <span className="muted"> Build a plan for the active project first.</span>}
        <ErrorNote error={act.error} />
        {r && (
          <>
            <h4>Recommendation: <Badge kind="ok">{r.recommendation}</Badge></h4>
            <Bars data={Object.fromEntries(Object.entries(r.scores).map(([k, v]) => [k, Math.max(0, v as number)]))} />
            <ul>{r.rationale.map((x: string) => <li key={x}>{x}</li>)}</ul>
            <p className="muted small">Scope is {(r.inputs.scope_ratio * 100).toFixed(1)}% of source rows. Method: {r.method}. Human approval required.</p>
          </>)}
      </Card>
    </>
  );
}

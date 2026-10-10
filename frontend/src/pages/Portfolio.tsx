import { useState } from "react";
import { api } from "../api";
import { useApi, useProject } from "../hooks";
import { Card, ErrorBox, Pill, Stat, Table } from "../components/ui";
import { fmtBytes, fmtNum } from "../api";

export default function Portfolio() {
  const q = useApi<any[]>("/platform/portfolio");
  const k = useApi<any>("/platform/portfolio/kpis");
  const { setProjectId } = useProject();
  const [scale, setScale] = useState(1);
  const [name, setName] = useState("Project Aurora - Specialty Materials carve-out");
  const [connector, setConnector] = useState("SYNTHETIC");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const create = async () => {
    setBusy(true); setErr(null);
    try { const r = await api("/projects/demo", { body: { scale, seed: 42, name, connector } }); setProjectId(r.project.id); q.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  return (
    <div>
      <Card title="Create project from synthetic ECC landscape" actions={<span className="muted">Imports a deterministic ECC 6.0 EHP8-like landscape (6 company codes, 9 plants) and a prepared S/4HANA 2025 shell target</span>}>
        <div className="row"><input style={{ minWidth: 360 }} value={name} onChange={(e) => setName(e.target.value)} /><label className="chk">Scale <input type="number" min={1} max={5} value={scale} onChange={(e) => setScale(Number(e.target.value))} style={{ width: 70 }} /></label><label className="chk">Source connector <select value={connector} onChange={(e) => setConnector(e.target.value)}><option value="SYNTHETIC">SYNTHETIC (record store)</option><option value="RFC">RFC source + API target (simulated SAP add-on and S/4HANA gateway)</option></select></label><button disabled={busy} onClick={create}>{busy ? "Importing…" : "Create demo project"}</button></div>
        <ErrorBox error={err} />
      </Card>
      {k.data && <Card title="Migration factory KPIs" actions={<span className="muted">{k.data.benchmarks.note}</span>}>
        <div className="stats">
          <Stat label="Projects" value={k.data.totals.projects} sub={Object.entries(k.data.totals.by_phase).map(([ph, n]) => `${ph.toLowerCase().replace(/_/g, " ")} ${n}`).join(" · ")} />
          <Stat label="Objects in scope" value={fmtNum(k.data.totals.objects_in_scope)} sub={`${fmtBytes(k.data.totals.est_bytes)} estimated · ${k.data.totals.approved_manifests} of ${k.data.totals.manifests} manifests approved`} />
          <Stat label="Runs" value={`${k.data.totals.completed_runs} / ${k.data.totals.runs}`} sub={`${k.data.totals.reconciled_pass} project(s) reconciled PASS`} />
          <Stat label="Cutover" value={`${k.data.totals.rehearsals_go} GO`} sub={`${k.data.totals.open_incidents} open incident(s)`} tone={k.data.totals.open_incidents ? "warn" : undefined} />
          <Stat label="Extraction rec/s" value={k.data.benchmarks.extraction_rec_s ? fmtNum(Math.round(k.data.benchmarks.extraction_rec_s.avg)) : "–"} sub={k.data.benchmarks.extraction_rec_s ? `min ${fmtNum(Math.round(k.data.benchmarks.extraction_rec_s.min))} · max ${fmtNum(Math.round(k.data.benchmarks.extraction_rec_s.max))} over ${k.data.benchmarks.completed_runs} run(s)` : "no completed run"} />
          <Stat label="Run seconds" value={k.data.benchmarks.run_seconds ? k.data.benchmarks.run_seconds.avg.toFixed(1) : "–"} sub={k.data.benchmarks.run_seconds ? `min ${k.data.benchmarks.run_seconds.min} · max ${k.data.benchmarks.run_seconds.max}` : ""} />
        </div>
      </Card>}
      <Card title="Portfolio">
        <ErrorBox error={q.error || k.error} />
        <Table cols={[{ k: "name", h: "Project", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setProjectId(r.id); }}>{r.name}</a> }, { k: "scenario_type", h: "Scenario" }, { k: "phase", h: "Phase", r: (r) => <Pill value={r.phase} /> }, { k: "manifests", h: "Manifests", r: (r) => `${r.approved_manifests} / ${r.manifests}` }, { k: "rulesets", h: "Rule sets", r: (r) => `${r.approved_rulesets} / ${r.rulesets}` }, { k: "objects_in_scope", h: "Objects", r: (r) => fmtNum(r.objects_in_scope) }, { k: "runs", h: "Runs", r: (r) => `${r.completed_runs} / ${r.runs}` }, { k: "last_reconciliation", h: "Reconciliation", r: (r) => <Pill value={r.last_reconciliation} /> }, { k: "last_extraction_rec_s", h: "Rec/s", r: (r) => r.last_extraction_rec_s ? fmtNum(Math.round(r.last_extraction_rec_s)) : "–" }, { k: "rehearsals", h: "Rehearsals", r: (r) => `${r.rehearsals_go} GO / ${r.rehearsals}` }, { k: "open_incidents", h: "Incidents" }, { k: "cleanup_plans", h: "Cleanup", r: (r) => `${r.cleanup_executed} / ${r.cleanup_plans}` }]} rows={k.data?.projects || q.data || []} />
      </Card>
    </div>
  );
}

import { useState } from "react";
import { api } from "../api";
import { useApi, useProject } from "../hooks";
import { Card, ErrorBox, Pill, Table } from "../components/ui";

export default function Portfolio() {
  const q = useApi<any[]>("/platform/portfolio");
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
      <Card title="Portfolio">
        <ErrorBox error={q.error} />
        <Table cols={[{ k: "name", h: "Project", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setProjectId(r.id); }}>{r.name}</a> }, { k: "scenario_type", h: "Scenario" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "manifests", h: "Manifests" }, { k: "approved_manifests", h: "Approved" }, { k: "objects_in_scope", h: "Objects in scope" }, { k: "runs", h: "Runs" }, { k: "completed_runs", h: "Completed" }, { k: "last_reconciliation", h: "Last reconciliation", r: (r) => <Pill value={r.last_reconciliation} /> }]} rows={q.data || []} />
      </Card>
    </div>
  );
}

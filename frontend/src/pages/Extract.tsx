import { useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Pill, Select, Table } from "../components/ui";

/** ABAP extraction: what the read-only agents selected for the company code in the latest run, per table. */
export default function Extract() {
  const { projectId, source, target } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const tables = useApi<any[]>("/catalog/tables");
  const [sel, setSel] = useState("");
  const run = (runs.data || []).find((r) => r.id === (sel || runs.data?.[0]?.id));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const approvedM = (manifests.data || []).find((m) => m.status === "APPROVED");
  const approvedR = (rulesets.data || []).find((r) => r.status === "APPROVED");
  const start = async () => {
    setBusy(true); setErr(null);
    try { const r = await api(`/projects/${projectId}/runs`, { body: { manifest_id: approvedM.id, ruleset_id: approvedR.id, mode: "SIMULATED", workers: 4, execution: "INLINE", pipelined: true, load_mode: "api" } }); runs.reload(); setSel(r.id); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  if (!projectId || !source) return <Banner>Select a project.</Banner>;
  const connected = source.connector !== "RFC" || source.meta?.rfc?.transport === "simulated" || source.connector_status !== "NOT_CONNECTED";
  const blocker = !approvedM ? "Approve a manifest first (Carve-out studio)." : !approvedR ? "Approve a ruleset first (Rules)." : !target ? "Register a target system first (Connect)." : !connected ? "Connect the ECC source before extraction (Connect)." : "";
  const stage = (run?.stages || []).find((s: any) => s.name === "EXTRACT");
  const byTable: Record<string, number> = stage?.metrics?.by_table || {};
  const desc = Object.fromEntries((tables.data || []).map((t) => [t.name, t]));
  const agent = source.connector === "RFC" ? "Z_SDTF_READ_PACKAGE" : "record store (synthetic)";
  const rows = Object.entries(byTable).sort((a, b) => a[0].localeCompare(b[0])).map(([table, n]) => ({ table, description: desc[table]?.description || "", rows: n, agent: `${agent}${desc[table]?.domain ? ` · ${desc[table].domain}` : ""}` }));
  const cp = stage?.checkpoint || {};
  return (
    <div>
      <div className="row"><button disabled={busy || !!blocker} onClick={start}>{busy ? "Starting the run…" : "Run extraction agents"}</button>{(runs.data || []).length > 0 && <Select value={run?.id} onChange={setSel} options={(runs.data || []).map((r) => ({ value: r.id, label: `${r.id.slice(0, 8)} ${r.status} ${String(r.started_at || "").slice(0, 16)}` }))} />}</div>
      {blocker && <p className="note-danger">{blocker}</p>}
      <ErrorBox error={err || runs.error} />
      {run && <p className="muted">Run {run.id.slice(0, 8)} <Pill value={run.status} /> · extraction <Pill value={stage?.status || "PENDING"} />{stage?.duration_ms ? ` · ${(stage.duration_ms / 1000).toFixed(1)} s` : ""} · manifest {approvedM?.name || run.manifest_id.slice(0, 8)} · an extraction run also transforms, loads and reconciles; follow it under Extract → Run monitor.</p>}
      <Card>
        <Table cols={[{ k: "table", h: "Table", r: (r) => <span className="mono">{r.table}</span> }, { k: "description", h: "Description" }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "agent", h: "Agent", r: (r) => <span className="mono">{r.agent}</span> }]} rows={rows} empty={run ? "No table counts recorded for this run's extraction yet." : "No run yet: start the extraction agents."} />
      </Card>
      {stage && <p className="muted">{cp.partitions_done !== undefined ? `Checkpoint: ${cp.partitions_done}/${cp.partitions_total ?? "?"} partitions. ` : Object.keys(cp).length ? `Checkpoint: ${JSON.stringify(cp).slice(0, 160)}. ` : ""}{stage.metrics?.rows !== undefined ? `${fmtNum(stage.metrics.rows)} rows staged. ` : ""}Sample document lines stay in the Finance view; the staged records are under Extract → Run monitor.</p>}
    </div>
  );
}

import { useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Select, Table } from "../components/ui";

export default function RunsMonitor() {
  const { projectId } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const [mid, setMid] = useState(""); const [rid, setRid] = useState(""); const [workers, setWorkers] = useState(4);
  const [sel, setSel] = useState("");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null);
  const run = (runs.data || []).find((r) => r.id === (sel || runs.data?.[0]?.id));
  const staged = useApi<any>(run ? `/runs/${run.id}/staged` : null, { limit: 20 }, [run?.id]);
  const start = async () => { setBusy(true); setErr(null); try { const r = await api(`/projects/${projectId}/runs`, { body: { manifest_id: mid, ruleset_id: rid, mode: "SIMULATED", workers } }); runs.reload(); setSel(r.id); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const resume = async () => { setBusy(true); setErr(null); try { await api(`/runs/${run.id}/resume`, { method: "POST" }); runs.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  return (
    <div>
      <Banner kind="warn">Runs are <b>SIMULATED</b>: extraction reads the synthetic record store, the loader writes into the simulated target. Production and rehearsal modes are refused by the engine.</Banner>
      <Card title="Start migration run" actions={<><Select value={mid} onChange={setMid} options={(manifests.data || []).filter((m) => m.status === "APPROVED").map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} placeholder="approved manifest" /><Select value={rid} onChange={setRid} options={(rulesets.data || []).filter((r) => r.status === "APPROVED").map((r) => ({ value: r.id, label: `${r.name} v${r.version}` }))} placeholder="approved ruleset" /><label className="chk">workers <input type="number" min={1} max={16} value={workers} onChange={(e) => setWorkers(Number(e.target.value))} style={{ width: 60 }} /></label><button disabled={busy || !mid || !rid} onClick={start}>{busy ? "Running…" : "Start simulated run"}</button></>}><ErrorBox error={err} /><p className="muted">Only APPROVED manifests and rulesets are offered. The engine re-verifies approval state and manifest hash before execution.</p></Card>
      <div className="grid2">
        <Card title="Runs"><Table cols={[{ k: "id", h: "Run", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setSel(r.id); }}>{r.id.slice(0, 8)}</a> }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "reconciliation", h: "Recon", r: (r) => <Pill value={r.reconciliation} /> }, { k: "started_by", h: "By" }, { k: "started_at", h: "Started", r: (r) => String(r.started_at || "").slice(0, 19) }]} rows={runs.data || []} /></Card>
        {run && <Card title={`Run ${run.id.slice(0, 8)} stages`} actions={run.status === "FAILED" && <button disabled={busy} onClick={resume}>Resume from checkpoint</button>}>
          <Table cols={[{ k: "name", h: "Stage" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "duration_ms", h: "ms" }, { k: "m", h: "Metrics", r: (r) => Object.entries(r.metrics || {}).filter(([, v]) => typeof v !== "object").map(([k, v]) => `${k}=${v}`).join(" · ") }]} rows={run.stages} />
          <KV obj={{ snapshot: run.snapshot_id, mode: run.mode, workers: run.metrics?.workers }} />
        </Card>}
      </div>
      {run && staged.data && <div className="grid2"><Card title="Staging by table and load status"><Table cols={[{ k: "table", h: "Table" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "count", h: "Records", r: (r) => fmtNum(r.count) }]} rows={staged.data.counts} /></Card><Card title="Staged record samples with lineage"><Table cols={[{ k: "table", h: "Table" }, { k: "key", h: "Source key" }, { k: "target_key", h: "Target key" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "lineage", h: "Lineage", r: (r) => r.lineage.map((l: any) => `${l.rule}:${l.field} ${l.from}→${l.to}`).join("; ") }]} rows={staged.data.items} /></Card></div>}
    </div>
  );
}

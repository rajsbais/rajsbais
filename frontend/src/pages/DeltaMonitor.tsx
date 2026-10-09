import { useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Select, Stat, Table } from "../components/ui";

export default function DeltaMonitor() {
  const { projectId, source } = useProjectDetails();
  const model = useApi<any>("/platform/delta/status");
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const baselines = (runs.data || []).filter((r) => r.status === "COMPLETED" && r.metrics?.kind !== "DELTA");
  const [sel, setSel] = useState("");
  const baseId = sel || baselines[0]?.id;
  const [tick, setTick] = useState(0);
  const state = useApi<any>(baseId ? `/runs/${baseId}/delta` : null, undefined, [baseId, tick]);
  const latest = state.data?.cycles?.slice(-1)[0];
  const [cycleId, setCycleId] = useState("");
  const shownCycle = cycleId || latest?.id;
  const events = useApi<any[]>(shownCycle ? `/runs/${shownCycle}/delta/events` : null, { limit: 300 }, [shownCycle, tick]);
  const [count, setCount] = useState(10);
  const [ccs, setCcs] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [last, setLast] = useState<any>(null);
  const simulated = source && (source.connector === "SYNTHETIC" || source.meta?.rfc?.transport === "simulated");
  const call = async (fn: () => Promise<any>) => { setBusy(true); setErr(null); try { setLast(await fn()); setTick((t) => t + 1); runs.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const simulate = () => call(() => api(`/systems/${source.id}/simulate-changes`, { body: { seed: Date.now() % 100000, count, company_codes: ccs ? ccs.split(",").map((x) => x.trim()).filter(Boolean) : null } }));
  const cycle = (final: boolean) => call(() => api(`/runs/${baseId}/delta/cycles`, { body: { final } }));
  const freeze = () => call(() => api(`/runs/${baseId}/delta/freeze`, { body: { note: "declared from Delta Synchronization Monitor" } }));
  if (!projectId) return <Banner>Select a project.</Banner>;
  const s = state.data;
  return (
    <div>
      <Banner kind="warn">Delta synchronisation is <b>SIMULATED</b>: change events come from the SAP add-on contract (<code>Z_SDTF_CDC_POLL</code>) served by the simulated add-on over the RFC adapter, and are loaded through the released S/4HANA APIs (business partner, product, sales order, purchase order, journal entry) on the simulated gateway. The engine's ordering, idempotency, conflict detection, API operations, freeze handling and final reconciliation are real; neither system is. No downtime figure is derived from these cycles.</Banner>
      <Card title="Baseline (initial load) run" actions={<Select value={baseId || ""} onChange={setSel} options={baselines.map((r) => ({ value: r.id, label: `${r.id.slice(0, 8)} · ${String(r.started_at || "").slice(0, 16)} · recon ${r.reconciliation || "-"}` }))} placeholder="completed baseline run" />}>
        {!baselines.length && <p className="muted">Complete an initial simulated run first (Extraction &amp; Load Monitor). Delta capture needs a source registered with the RFC connector; create the demo project with “RFC (simulated SAP add-on)” in Portfolio.</p>}
        {s && !s.cdc_supported && <ErrorBox error={`Source ${s.source.sid} uses the ${s.source.connector} connector, which has no change log. Delta capture needs the RFC connector (Z_SDTF_CDC_POLL on the SAP add-on).`} />}
        {s && s.cdc_supported && <>
          <div className="stats">
            <Stat label="Watermark" value={s.watermark} sub={`source ${s.source.sid} · ${s.source.transport || "pyrfc"}`} />
            <Stat label="Backlog at source" value={s.backlog?.error ? "n/a" : fmtNum(s.backlog?.events)} sub={s.backlog?.error ? s.backlog.error : `${fmtNum(s.backlog?.in_scope)} in scope${s.backlog?.lag_seconds != null ? ` · lag ${s.backlog.lag_seconds}s` : ""}`} />
            <Stat label="Cycles" value={s.cycles.length} sub={latest ? `last: ${latest.status}, recon ${latest.reconciliation || "-"}` : "none yet"} />
            <Stat label="Business freeze" value={<Pill value={s.freeze ? "DECLARED" : "OPEN"} />} sub={s.freeze ? `${s.freeze.declared_by} · ${String(s.freeze.declared_at).slice(0, 16)}` : "changes keep flowing"} />
            <Stat label="Cutover readiness" value={<Pill value={s.cutover_ready ? "READY" : s.final_delta ? "FINAL DELTA FAILED" : "PENDING"} />} sub={s.final_delta ? `final cycle ${String(s.final_delta.run_id).slice(0, 8)}` : "final delta + full reconciliation outstanding"} />
          </div>
          <div className="row" style={{ marginTop: 10 }}>
            {simulated && <><label className="chk">Simulate business activity: <input type="number" min={1} max={200} value={count} onChange={(e) => setCount(Number(e.target.value))} style={{ width: 70 }} /> changes</label><input placeholder="company codes, e.g. 5000,1000 (default: all)" value={ccs} onChange={(e) => setCcs(e.target.value)} style={{ minWidth: 260 }} /><button className="secondary" disabled={busy || !source} onClick={simulate}>Simulate activity</button></>}
            <button disabled={busy || !!s.final_delta} onClick={() => cycle(false)}>{busy ? "Working…" : "Run delta cycle"}</button>
            <button className="secondary" disabled={busy || !!s.freeze} onClick={freeze}>Declare business freeze</button>
            <button disabled={busy || !s.freeze || !!s.final_delta} onClick={() => cycle(true)} title={!s.freeze ? "declare the business freeze first" : ""}>Final delta + full reconciliation</button>
          </div>
          <ErrorBox error={err} />
          {last && <p className="muted">Last action: {Object.entries(last).filter(([, v]) => typeof v !== "object").map(([k, v]) => `${k}=${v}`).join(" · ")}</p>}
        </>}
      </Card>
      {s && s.cdc_supported && <div className="grid2">
        <Card title="Delta cycles">
          <Table cols={[{ k: "cycle", h: "#", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setCycleId(r.id); }}>{r.cycle}{r.final ? " (final)" : ""}</a> }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "captured", h: "Captured" }, { k: "in_scope", h: "In scope" }, { k: "filtered", h: "Filtered" }, { k: "applied", h: "Applied" }, { k: "conflicts", h: "Conflicts" }, { k: "rejected", h: "Rejected" }, { k: "lag_seconds", h: "Lag s" }, { k: "reconciliation", h: "Recon", r: (r) => <Pill value={r.reconciliation} /> }, { k: "final_reconciliation", h: "Final recon", r: (r) => (r.final_reconciliation ? <Pill value={r.final_reconciliation} /> : "") }, { k: "duration_ms", h: "ms" }]} rows={s.cycles} />
        </Card>
        <Card title="Near-zero-downtime stage model">
          {model.data && <Table cols={[{ k: "stage", h: "Stage" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }]} rows={model.data.stages.map((st: string) => ({ stage: st, status: model.data.stage_status?.[st] || "PLANNED" }))} />}
          <p className="muted">{model.data?.note}</p>
        </Card>
      </div>}
      {shownCycle && events.data && <Card title={`Events of cycle ${shownCycle.slice(0, 8)} (${events.data.length})`}>
        <Table cols={[{ k: "seq", h: "Seq" }, { k: "changenr", h: "Change set" }, { k: "object_type", h: "Object" }, { k: "table", h: "Table" }, { k: "record_key", h: "Source key" }, { k: "op", h: "Op" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "action", h: "Action" }, { k: "load_method", h: "Load method" }, { k: "api_call", h: "API call" }, { k: "target_key", h: "Target key" }, { k: "message", h: "Reason / message" }]} rows={events.data} />
      </Card>}
      {s?.freeze && <Card title="Business freeze"><KV obj={s.freeze} /></Card>}
    </div>
  );
}

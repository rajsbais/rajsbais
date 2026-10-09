import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Bars, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const describe = (s: J) => (!s ? "manual only" : s.kind === "weekly" ? `weekly ${DAYS[s.weekday]} ${String(s.hour).padStart(2, "0")}:00 UTC, ${s.window_hours ?? 4}h window` : `every ${s.every_hours}h`);

export default function Delta() {
  const app = useApp();
  const [list, setList] = useState<J[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [hist, setHist] = useState<J[]>([]);
  const [prev, setPrev] = useState<J>(null);
  const [rec, setRec] = useState<J[]>([]);
  const [src, setSrc] = useState(""); const [tgt, setTgt] = useState("");
  const act = useAction();
  const sc = list.find((x) => x.id === sel) ?? null;

  const load = useCallback(async () => {
    const l = await api.get("/api/delta/scenarios"); setList(l);
    const id = sel && l.some((x: J) => x.id === sel) ? sel : l[0]?.id ?? null; setSel(id);
    setHist(id ? await api.get(`/api/delta/scenarios/${id}/history`) : []);
  }, [sel]);
  useEffect(() => { act.run(load); }, [sel, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setPrev(null); setRec([]); }, [sel]);

  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); });
  const post = (path: string, body?: unknown) => go(() => api.post(`/api/delta/scenarios/${sc!.id}/${path}`, body ?? {}));
  const pairs = [["ECC", app.systems.filter((s) => s.family === "ECC")], ["S4", app.systems.filter((s) => s.family === "S4")]] as const;
  const srcFam = app.systems.find((s) => s.id === src)?.family;

  return (
    <>
      <Card title="Delta refresh scenarios" actions={<button onClick={() => act.run(load)}>Refresh</button>}>
        <p className="muted">Standing, approved, repeatable refreshes. Each run finds what changed in the source, checks the target for edits, and loads only the difference. Not every object supports the same delta mechanism: see “By mechanism”.</p>
        <div className="form">
          <label>Source<select value={src} onChange={(e) => { setSrc(e.target.value); setTgt(""); }}><option value="">choose…</option>
            {pairs.flatMap(([, ss]) => ss).map((s: J) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select></label>
          <label>Target (writable)<select value={tgt} onChange={(e) => setTgt(e.target.value)}><option value="">choose…</option>
            {app.systems.filter((s) => s.writable_target && s.family === srcFam).map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
        </div>
        <button className="primary" disabled={!src || !tgt || act.busy || !app.can("plan:write")}
                onClick={() => go(async () => { const c = await api.post(`/api/delta/scenarios/demo?source_id=${src}&target_id=${tgt}`); setSel(c.id); })}>
          Create weekend QA sync (customers, vendors, materials, sales &amp; purchase orders · company 1000)
        </button>
        <ErrorNote error={act.error} />
        <DataTable rows={list} cols={[
          { key: "name", title: "Scenario", render: (r) => <button onClick={() => setSel(r.id)}>{r.name.slice(0, 48)}</button> },
          { key: "status", title: "Status", render: (r) => <StatusBadge s={r.status} /> },
          { key: "schedule", title: "Schedule", render: (r) => describe(r.schedule) },
          { key: "next_due", title: "Next due", render: (r) => r.next_due ?? "—" },
          { key: "runs", title: "Runs" }]} empty="No scenarios yet" pageSize={5} />
      </Card>

      {sc && (
        <>
          <div className="grid stats">
            <Stat label="Status" value={<StatusBadge s={sc.status} />} hint={`config v${sc.config_version}`} />
            <Stat label="Watermark (change seq)" value={sc.watermark ?? "—"} hint="advances only after a clean gate" />
            <Stat label="Tracked objects" value={sc.tracked_objects} hint={`${sc.deferred_objects} deferred · ${sc.stale_objects} stale`} />
            <Stat label="Next due" value={<span className="small mono">{sc.next_due ?? "—"}</span>} hint={describe(sc.schedule)} />
          </div>
          <Card title="Operate">
            <div className="row">
              <button disabled={act.busy || sc.status !== "DRAFT" || !app.can("plan:submit")} onClick={() => post("submit")}>Submit for approval</button>
              <button className="primary" disabled={act.busy || sc.status !== "PENDING_APPROVAL" || !app.can("plan:approve")} onClick={() => post("approve")}>Approve</button>
              <button disabled={act.busy || !app.can("plan:write")} onClick={() => act.run(async () => setPrev(await api.post(`/api/delta/scenarios/${sc.id}/preview`, {})))}>Preview delta</button>
              <button className="primary" disabled={act.busy || sc.status !== "APPROVED" || !app.can("run:execute")} onClick={() => post("run")}>Run now</button>
              <button disabled={act.busy || sc.status !== "APPROVED" || !app.can("run:execute")} onClick={() => post("run", { full_sweep: true })}>Run full sweep</button>
              <button disabled={act.busy || sc.status !== "FAILED" || !app.can("run:execute")} onClick={() => post("resume")}>Resume</button>
              <button className="danger" disabled={act.busy || !["FAILED", "ATTENTION"].includes(sc.status) || !app.can("run:execute")}
                      onClick={() => confirm("Roll back the last run's changes in the target?") && post("rollback")}>Roll back</button>
              <button disabled={act.busy || sc.status !== "ATTENTION" || !app.can("run:execute")}
                      onClick={() => { const note = prompt("Justification for accepting the held run (audited):"); if (note) void post("acknowledge", { note }); }}>Acknowledge held run</button>
            </div>
            <p className="muted small">Approval is bound to the configuration hash <code>{sc.config_hash.slice(0, 12)}…</code>; any change to scope, policy or masking rules requires re-approval. Approver ≠ creator/editor/submitter; AI agents cannot approve.</p>
            <div className="row">
              <button disabled={act.busy} onClick={() => act.run(async () => setRec(await api.get(`/api/delta/scenarios/${sc.id}/masking/recommended`)))}>Check masking coverage</button>
              {rec.length > 0 && <button disabled={!app.can("masking:write") || act.busy} onClick={() => go(async () => { await api.post(`/api/delta/scenarios/${sc.id}/masking/rules`, { rules: rec }); setRec([]); })}>
                Add {rec.length} recommended rule(s) (requires re-approval)</button>}
              {rec.length > 0 && <span className="muted small">{rec.map((r) => `${r.table}.${r.field}`).join(", ")}</span>}
            </div>
            <details><summary className="muted small">Demo and scheduler controls (simulation only)</summary>
              <div className="row">
                <button disabled={act.busy || !app.can("system:write")} onClick={() => go(async () => { await api.post(`/api/demo/simulate-source-changes?system_id=${sc.source_id}`); })}>Simulate source activity</button>
                <button disabled={act.busy || !app.can("run:execute")} onClick={() => go(async () => { const r = await api.post("/api/delta/tick", {}); alert(r.length ? JSON.stringify(r) : "Scheduler tick: nothing due"); })}>Scheduler tick (now)</button>
              </div></details>
          </Card>
        </>)}

      {prev && (
        <Card title={`Preview: ${prev.kind} · ${prev.mode.replace("_", " ")} · change seq ${prev.from_seq ?? "—"} → ${prev.to_seq}`}>
          <div className="grid stats">
            <Stat label="New" value={prev.new} /><Stat label="Changed" value={prev.changed} /><Stat label="Target drift" value={prev.target_drift} />
            <Stat label="Re-evaluated (deferred)" value={prev.retried_deferred} /><Stat label="Quarantined / skipped" value={`${prev.quarantined} / ${prev.skipped}`} />
            <Stat label="Out of window (kept)" value={prev.retained_out_of_scope} hint={`${prev.deleted_in_source} deleted in source: not propagated`} />
          </div>
          <div className="grid two"><div><h3>By mechanism</h3><Bars data={prev.by_mechanism} /></div>
            <div><h3>Blocking</h3>{prev.blocking.length ? <ul className="checks">{prev.blocking.map((b: string) => <li key={b}><StatusBadge s="blocking" /> {b}</li>)}</ul> : <Badge kind="ok">none</Badge>}
              {prev.notes.map((n: string) => <p key={n} className="muted small">{n}</p>)}</div></div>
        </Card>)}

      <Card title="Execution history (versioned)">
        <DataTable rows={hist} cols={[
          { key: "version", title: "v" }, { key: "kind", title: "Kind" }, { key: "trigger", title: "Trigger" }, { key: "mode", title: "Mode" },
          { key: "status", title: "Status", render: (h) => <StatusBadge s={h.status} /> },
          { key: "release", title: "Gate", render: (h) => <StatusBadge s={h.release === "NOT_EVALUATED" ? "info" : h.release} /> },
          { key: "seq", title: "Change seq", render: (h) => `${h.from_seq ?? "—"} → ${h.to_seq}` },
          { key: "counts", title: "New / changed / drift", render: (h) => `${h.new} / ${h.changed} / ${h.target_drift}` },
          { key: "loaded", title: "Loaded" },
          { key: "run_id", title: "Run", render: (h) => (h.run_id ? <button onClick={() => { app.setRunId(h.run_id); app.go("reconciliation"); }}>{h.run_id}</button> : "—") }]}
          empty="No runs yet" pageSize={8} />
      </Card>
    </>
  );
}

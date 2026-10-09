import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const KIND: Record<string, string> = { SUCCEEDED: "pass", FAILED: "fail", DEAD: "fail", ATTENTION: "warn", CANCELLED: "info", RUNNING: "info" };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DEMO_PIPELINE = { name: "Nightly hygiene", steps: [
  { key: "lean", kind: "lean_sweep" }, { key: "tdm", kind: "tdm_sweep" },
  { key: "audit", kind: "agent_run", params: { agent_id: "compliance-verification" }, after: ["lean", "tdm"] },
  { key: "review", kind: "human_gate", params: { note: "Review the compliance report before the morning", escalate_after_hours: 8 }, after: ["audit"] }] };

export default function Orchestration() {
  const app = useApp();
  const [sum, setSum] = useState<J>(null); const [jobs, setJobs] = useState<J[]>([]); const [sch, setSch] = useState<J[]>([]);
  const [events, setEvents] = useState<J[]>([]); const [outbox, setOutbox] = useState<J[]>([]); const [subs, setSubs] = useState<J[]>([]);
  const [target, setTarget] = useState(""); const [win, setWin] = useState<J>(null);
  const [days, setDays] = useState<number[]>([4, 5]); const [start, setStart] = useState("22:00"); const [end, setEnd] = useState("06:00");
  const [last, setLast] = useState<J>(null);
  const act = useAction();
  const targets = app.systems.filter((s) => s.role !== "PRD");

  const load = useCallback(async () => {
    setSum(await api.get("/api/orchestration/summary")); setJobs(await api.get("/api/orchestration/jobs")); setSch(await api.get("/api/orchestration/schedules"));
    setEvents(await api.get("/api/orchestration/events?limit=30")); setOutbox(await api.get("/api/orchestration/outbox")); setSubs(await api.get("/api/orchestration/subscriptions"));
    if (target) setWin(await api.get(`/api/orchestration/windows/${target}`));
  }, [target]);
  useEffect(() => { if (!target && targets[0]) setTarget(targets[0].id); }, [targets.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); });
  const post = (p: string, b?: unknown) => go(() => api.post(p, b ?? {}));

  return (
    <>
      <Card title="Orchestration" actions={<div className="row">
        <button className="primary" disabled={act.busy} onClick={() => go(async () => setLast(await api.post("/api/orchestration/tick")))}>Run scheduler tick</button>
        <button disabled={!app.can("system:write") || act.busy} onClick={() => post("/api/orchestration/clock", { hours: 6 })}>Simulation: advance clock +6h</button></div>}>
        <p className="muted">The orchestrator decides <strong>when</strong> and <strong>in what order</strong>. It adds no authority: every job still needs the approvals of the module it calls, and runs as a service account that can only execute what is already approved. Jobs run one at a time; the queue is in memory; an external scheduler must call the tick; notifications are recorded, not sent.</p>
        {sum && <div className="grid stats">
          <Stat label="Clock" value={sum.simulated_clock ? "simulated" : "wall clock"} hint={sum.now?.slice(0, 16).replace("T", " ")} />
          {Object.entries(sum.jobs).map(([k, v]) => <Stat key={k} label={k.replace("_", " ").toLowerCase()} value={v as number} />)}
          <Stat label="Target leases" value={Object.keys(sum.leases).length} hint={Object.values(sum.leases).map((l: J) => `${l.kind} ${l.holder}`).join(", ") || "none"} />
        </div>}
        {last && <p className="muted small">Last tick: ran {last.ran.length}, waiting {last.waiting.length}, fired {last.fired.length}, missed {last.missed.length}.</p>}
        <ErrorNote error={act.error} />
      </Card>

      <Card title="Jobs and pipelines" actions={<button disabled={!app.can("run:execute")} onClick={() => post("/api/orchestration/pipelines", DEMO_PIPELINE)}>Submit demo pipeline</button>}>
        <DataTable rows={jobs} empty="No jobs yet. Submit the demo pipeline, then run a tick." cols={[
          { key: "id", title: "Job" }, { key: "kind", title: "Kind" }, { key: "step", title: "Step", render: (j: J) => j.step ?? "—" },
          { key: "status", title: "Status", render: (j: J) => <StatusBadge s={KIND[j.status] ?? (j.status.startsWith("WAITING") || j.status === "RETRY_WAIT" ? "warn" : "info")} /> },
          { key: "st", title: "State", render: (j: J) => <span>{j.status}{j.attempts > 1 ? ` · attempt ${j.attempts}` : ""}</span> },
          { key: "why", title: "Why", render: (j: J) => <span className="muted small">{j.reasons[0] ?? (j.events.at(-1)?.msg ?? "")}</span> },
          { key: "act", title: "", render: (j: J) => (
            <div className="row">
              {j.kind === "human_gate" && j.status === "WAITING_HUMAN" && <button disabled={act.busy} onClick={() => post(`/api/orchestration/jobs/${j.id}/complete`, { note: "reviewed" })}>Complete gate</button>}
              {!["SUCCEEDED", "FAILED", "DEAD", "CANCELLED", "ATTENTION", "RUNNING"].includes(j.status) && <button disabled={act.busy} onClick={() => post(`/api/orchestration/jobs/${j.id}/cancel`)}>Cancel</button>}
            </div>) }]} />
      </Card>

      <div className="grid two">
        <Card title="Maintenance window" actions={<select aria-label="Window target" value={target} onChange={(e) => setTarget(e.target.value)}>{targets.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select>}>
          {win && <p><Badge kind={win.open ? "ok" : "warn"}>{win.open ? "open" : "closed"}</Badge> <span className="muted">{win.reason}{win.next_open ? ` · opens ${win.next_open.slice(0, 16).replace("T", " ")} UTC` : ""}</span></p>}
          <div className="row">{DAYS.map((d, i) => <label key={d} className="check"><input type="checkbox" checked={days.includes(i)} onChange={(e) => setDays(e.target.checked ? [...days, i] : days.filter((x) => x !== i))} />{d}</label>)}</div>
          <div className="form"><label>Starts (UTC)<input aria-label="Window start" value={start} onChange={(e) => setStart(e.target.value)} /></label><label>Ends (UTC)<input aria-label="Window end" value={end} onChange={(e) => setEnd(e.target.value)} /></label></div>
          <p className="muted small">An end earlier than the start wraps past midnight and belongs to the weekday it starts on. Blackouts override allowed windows. No rule = unrestricted.</p>
          <div className="row">
            <button disabled={!app.can("system:write") || !target} onClick={() => go(() => api.put(`/api/orchestration/windows/${target}`, { allow: [{ weekdays: days, start, end }], blackouts: win?.rules?.blackouts ?? [] }))}>Save window</button>
            <button disabled={!app.can("system:write") || !target} onClick={() => go(() => api.put(`/api/orchestration/windows/${target}`, { allow: win?.rules?.allow ?? [], blackouts: [{ from: new Date(Date.now() - 36e5).toISOString(), to: new Date(Date.now() + 12 * 36e5).toISOString(), reason: "change freeze" }] }))}>Add 12h freeze</button>
            <button disabled={!app.can("system:write") || !target} onClick={() => go(() => api.put(`/api/orchestration/windows/${target}`, {}))}>Clear</button></div>
        </Card>
        <Card title="Schedules" actions={<button disabled={!app.can("run:execute")} onClick={() => post("/api/orchestration/schedules", { name: "Nightly hygiene 02:00", schedule: { kind: "daily", hour: 2, window_hours: 3 }, template: { type: "pipeline", steps: DEMO_PIPELINE.steps } })}>New nightly schedule</button>}>
          <DataTable rows={sch} empty="No schedules." cols={[
            { key: "name", title: "Name" }, { key: "next", title: "Next run", render: (s: J) => s.next_due?.slice(0, 16).replace("T", " ") ?? "—" },
            { key: "st", title: "State", render: (s: J) => <Badge kind={s.approved ? (s.paused ? "info" : "ok") : "warn"}>{!s.approved ? "needs approval" : s.paused ? "paused" : "active"}</Badge> },
            { key: "a", title: "", render: (s: J) => (<div className="row">
              {!s.approved && <button disabled={!app.can("plan:approve") || act.busy} title="A different approver than the creator" onClick={() => post(`/api/orchestration/schedules/${s.id}/approve`)}>Approve</button>}
              <button disabled={!app.can("run:execute") || act.busy} onClick={() => post(`/api/orchestration/schedules/${s.id}/${s.paused ? "resume" : "pause"}`)}>{s.paused ? "Resume" : "Pause"}</button></div>) }]} />
        </Card>
      </div>

      <div className="grid two">
        <Card title="Event feed">
          <ul className="plain">{events.map((e) => <li key={e.id}><Badge kind={e.severity === "error" ? "bad" : e.severity === "warn" ? "warn" : "info"}>{e.severity}</Badge> <span className="small">{e.subject}</span> <span className="muted small">{e.ts.slice(11, 16)}</span></li>)}
            {!events.length && <li className="muted">No events yet.</li>}</ul>
        </Card>
        <Card title="Notifications" actions={<button disabled={!app.can("system:write")} onClick={() => post("/api/orchestration/subscriptions", { channel: "webhook", destination: "https://hooks.example.test/refresh", min_severity: "warn" })}>Subscribe a webhook (demo)</button>}>
          <p className="muted small">Channel: {sum?.notifier}. Messages are recorded in the outbox below and <strong>never sent</strong>; they carry ids and statuses only, no data.</p>
          <p className="small">{subs.filter((s) => s.active).length} active subscription(s).</p>
          <ul className="plain">{outbox.slice(0, 8).map((o) => <li key={o.id} className="small"><StatusBadge s={o.status.startsWith("DELIVERED") ? "pass" : o.status === "DROPPED" ? "fail" : "info"} /> {o.channel} → {o.destination}: {o.subject}</li>)}
            {!outbox.length && <li className="muted">Outbox empty.</li>}</ul>
        </Card>
      </div>
    </>
  );
}

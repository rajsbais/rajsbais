import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Pill, Stat, Table } from "./ui";

const hm = (s: string | null | undefined) => (s ? String(s).slice(11, 16) : "–");
const dmy = (s: string | null | undefined) => (s ? String(s).slice(5, 16).replace("T", " ") : "–");
const min = (n: number | null | undefined) => (n === null || n === undefined ? "–" : `${Math.round(n)} min`);

/** Live execution of a started rehearsal: the runbook against the clock, the downtime clock, incidents and assignments. */
export function LiveExecution({ rehearsalId, active, onChanged }: { rehearsalId: string; active: boolean; onChanged?: () => void }) {
  const [tl, setTl] = useState<any>(null);
  const [full, setFull] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => { api(`/cutover/rehearsals/${rehearsalId}/timeline`).then(setTl).catch((e: any) => setErr(e.message)); api(`/cutover/rehearsals/${rehearsalId}`).then(setFull).catch(() => {}); };
  useEffect(() => { setTl(null); setFull(null); setErr(null); load(); const h = active ? window.setInterval(load, 30000) : undefined; return () => { if (h) window.clearInterval(h); }; }, [rehearsalId, active]);
  const call = async (fn: () => Promise<any>) => { setErr(null); try { await fn(); load(); onChanged?.(); } catch (e: any) { setErr(e.message); } };
  const assign = (t: any) => { const assignee = window.prompt(`Who runs ${t.id} ${t.name}? (empty clears)`, t.assignee || ""); if (assignee === null) return; const backup = assignee ? window.prompt("Backup (optional):", t.backup || "") ?? "" : ""; const contact = assignee ? window.prompt("How to reach them (channel, bridge; never a credential):", t.contact || "") ?? "" : ""; call(() => api(`/cutover/rehearsals/${rehearsalId}/assignments/${t.id}`, { method: "PUT", body: { assignee, backup, contact } })); };
  const raise_ = (t: any) => { const title = window.prompt(`Incident on ${t.id} ${t.name}: what happened?`, ""); if (!title) return; const severity = (window.prompt("Severity: LOW, MEDIUM, HIGH or CRITICAL (HIGH and CRITICAL block GO; CRITICAL is escalated at once)", "MEDIUM") || "").toUpperCase(); if (!severity) return; const detail = window.prompt("Detail (optional):", "") ?? ""; call(() => api(`/cutover/rehearsals/${rehearsalId}/incidents`, { body: { task: t.id, severity, title, detail } })); };
  const escalate = (i: any) => { const note = window.prompt(`Escalate ${i.id} to the next level (${(i.path || [])[i.level] || "a named person"}): note`, ""); if (note === null) return; const to = window.prompt("Or name the person / role to escalate to (empty for the next level):", "") ?? ""; call(() => api(`/cutover/rehearsals/${rehearsalId}/incidents/${i.id}/escalate`, { body: { note, to } })); };
  const resolve = (i: any) => { const resolution = window.prompt(`Resolve ${i.id}: what was done?`, ""); if (!resolution) return; call(() => api(`/cutover/rehearsals/${rehearsalId}/incidents/${i.id}/resolve`, { body: { resolution } })); };
  if (!tl) return <ErrorBox error={err} />;
  const dt = tl.downtime;
  const incidents: any[] = full?.incidents || [];
  const open = incidents.filter((i) => i.status !== "RESOLVED");
  return (
    <Card title="Live execution" actions={<><Pill value={tl.status} />{dt.running && <Pill value="DOWNTIME RUNNING" />}<button className="secondary" onClick={load}>Refresh</button></>}>
      <div className="stats">
        <Stat label="Elapsed" value={min(tl.elapsed_minutes)} sub={`planned ${min(tl.planned_total_minutes)} · projected ${min(tl.projected_total_minutes)}`} tone={tl.projected_total_minutes > tl.planned_total_minutes * 1.1 ? "warn" : undefined} />
        <Stat label="Downtime clock" value={dt.started_at ? min(dt.elapsed_minutes) : "not started"} sub={`planned ${min(dt.planned_minutes)} · projected ${min(dt.projected_minutes)}${dt.started_at ? ` · since ${hm(dt.started_at)}` : ""}${dt.ended_at ? ` · ended ${hm(dt.ended_at)}` : ""}`} tone={dt.running ? (dt.projected_minutes > dt.planned_minutes ? "bad" : "warn") : undefined} />
        <Stat label="Tasks" value={`${tl.counts.DONE} / ${tl.tasks.length} done`} sub={`${tl.counts.RUNNING} running · ${tl.counts.READY} ready · ${tl.late.length} late${tl.next.length ? ` · next ${tl.next.join(", ")}` : ""}`} tone={tl.late.length ? "warn" : undefined} />
        <Stat label="Incidents open" value={tl.incidents.open} sub={tl.incidents.blocking.length ? `blocking GO: ${tl.incidents.blocking.join(", ")}` : "none blocking"} tone={tl.incidents.blocking.length ? "bad" : tl.incidents.open ? "warn" : "good"} />
        <Stat label="Assigned" value={`${tl.assignments.assigned} / ${tl.assignments.tasks}`} sub={tl.assignments.unassigned_downtime.length ? `downtime tasks without an owner: ${tl.assignments.unassigned_downtime.join(", ")}` : "every downtime task has an owner"} tone={tl.assignments.unassigned_downtime.length ? "warn" : "good"} />
      </div>
      <ErrorBox error={err} />
      <Table cols={[
        { k: "id", h: "#" },
        { k: "name", h: "Task", r: (t) => <><b>{t.name}</b>{t.downtime && <span className="muted"> · downtime</span>}{t.irreversible && <> <Pill value="IRREVERSIBLE" /></>}</> },
        { k: "status", h: "Status", r: (t) => <><Pill value={t.status} />{t.late_minutes > 0 && <div className="note-danger" style={{ margin: 0 }}>late {Math.round(t.late_minutes)} min</div>}</> },
        { k: "plan", h: "Planned", r: (t) => t.planned_start ? `${hm(t.planned_start)} – ${hm(t.planned_finish)}` : `+${t.planned_offset_min} min` },
        { k: "act", h: "Actual", r: (t) => t.started_at ? <>{hm(t.started_at)} – {hm(t.finished_at)}{t.actual_minutes !== null && <> · {min(t.actual_minutes)}{t.variance_minutes !== null && <span className={t.variance_minutes > 0 ? "note-danger" : "muted"}> ({t.variance_minutes > 0 ? "+" : ""}{t.variance_minutes})</span>}</>}<div className="muted" style={{ fontSize: 12 }}>{t.observed ? `observed: ${t.source}` : `by ${t.timed_by}`}</div></> : t.history ? <span className="muted" style={{ fontSize: 12 }}>history: {t.history.source} ({min(t.history.actual_minutes)}, {dmy(t.history.started_at)})</span> : <span className="muted">est. {min(t.est_minutes)} · proj. {hm(t.projected_finish)}</span> },
        { k: "assignee", h: "Assignee", r: (t) => t.assignee ? <>{t.assignee}{t.backup && <span className="muted"> · backup {t.backup}</span>}{t.contact && <div className="muted" style={{ fontSize: 12 }}>{t.contact}</div>}</> : <span className="muted">{t.owner}</span> },
        { k: "inc", h: "Incidents", r: (t) => t.open_incidents ? <Pill value={t.highest_open_severity} /> : "" },
        { k: "x", h: "", r: (t) => active ? <span className="grid-actions"><button className="secondary" onClick={() => assign(t)}>Assign</button><button className="secondary" onClick={() => raise_(t)}>Raise incident</button></span> : "" },
      ]} rows={tl.tasks} keyFn={(t) => t.id} />
      <p className="muted">Planned windows start at the rehearsal's start and follow the dependencies and estimates; a task is observed when the platform did the work itself in a run started during this rehearsal (hand timings win); history is a run from before the start. Projection: running tasks finish no earlier than now, the rest follow their dependencies. Nothing here reads an SAP system, a scheduler or a ticketing tool, and nobody is paged by the platform.</p>
      {incidents.length > 0 && <>
        <h4>Incidents ({open.length} open)</h4>
        <Table cols={[
          { k: "id", h: "Incident" }, { k: "task", h: "Task" }, { k: "severity", h: "Severity", r: (i) => <Pill value={i.severity} /> }, { k: "status", h: "Status", r: (i) => <Pill value={i.status} /> },
          { k: "title", h: "What", r: (i) => <><b>{i.title}</b>{i.detail && <div className="muted" style={{ fontSize: 12 }}>{i.detail}</div>}</> },
          { k: "raised", h: "Raised", r: (i) => `${i.raised_by} ${dmy(i.raised_at)}` },
          { k: "esc", h: "Escalation", r: (i) => (i.escalations || []).length ? (i.escalations as any[]).map((e) => `${e.level}: ${e.to} (${hm(e.at)})`).join(" → ") : <span className="muted">path: {(i.path || []).join(" → ")}</span> },
          { k: "res", h: "Resolution", r: (i) => i.resolution ? `${i.resolution} (${i.resolved_by}, ${dmy(i.resolved_at)})` : "" },
          { k: "x", h: "", r: (i) => active && i.status !== "RESOLVED" ? <span className="grid-actions"><button className="secondary" onClick={() => escalate(i)}>Escalate</button><button onClick={() => resolve(i)}>Resolve</button></span> : "" },
        ]} rows={incidents} keyFn={(i) => i.id} />
      </>}
    </Card>
  );
}

import { useEffect, useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Pre, Select, Stat, Table } from "../components/ui";

export default function Cutover() {
  const { projectId } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const [mid, setMid] = useState("");
  const id = mid || manifests.data?.[0]?.id;
  const rb = useApi<any>(id ? `/manifests/${id}/cutover/runbook` : null, undefined, [id]);
  const rehearsals = useApi<any[]>(id ? `/manifests/${id}/cutover/rehearsals` : null, undefined, [id]);
  const [rid, setRid] = useState("");
  const [reh, setReh] = useState<any>(null);
  const [report, setReport] = useState<string | null>(null);
  const [risk, setRisk] = useState<any>(null); const [err, setErr] = useState<string | null>(null);
  const [kind, setKind] = useState("MOCK");
  useEffect(() => { setRid(""); setReh(null); setReport(null); }, [id]);
  useEffect(() => { if (!rid && rehearsals.data?.length) setRid(rehearsals.data[rehearsals.data.length - 1].id); }, [rehearsals.data, rid]);
  useEffect(() => { if (!rid) { setReh(null); return; } api(`/cutover/rehearsals/${rid}`).then(setReh).catch((e: any) => setErr(e.message)); }, [rid]);
  const call = async (fn: () => Promise<any>, after?: (x: any) => void) => { setErr(null); try { const x = await fn(); if (after) after(x); } catch (e: any) { setErr(e.message); } };
  const assess = () => call(() => api(`/projects/${projectId}/agents/cutover_risk/run`, { body: { context: { manifest_id: id } } }), setRisk);
  const createRehearsal = () => { const name = window.prompt(`Name of the ${kind === "MOCK" ? "mock cutover" : kind === "DRESS" ? "dress rehearsal" : "go-live checklist"}:`, ""); if (name === null) return; call(() => api(`/manifests/${id}/cutover/rehearsals`, { body: { name, kind } }), (x) => { rehearsals.reload(); setRid(x.id); setReh(x); setReport(null); }); };
  const refreshReh = (x?: any) => { if (x && x.items) setReh(x); else if (rid) api(`/cutover/rehearsals/${rid}`).then(setReh); rehearsals.reload(); rb.reload(); };
  const post = (path: string, body?: any) => call(() => api(`/cutover/rehearsals/${rid}${path}`, { body: body || {} }), refreshReh);
  const markItem = (it: any, status: string) => { const needsNote = status === "FAIL" || (it.kind === "AUTO" && status === "NOT_APPLICABLE"); const note = window.prompt(status === "FAIL" ? "What failed?" : status === "NOT_APPLICABLE" ? (it.kind === "AUTO" ? "Why is this check waived?" : "Why is it not applicable?") : "Note (optional):", ""); if (note === null || (needsNote && !note.trim())) return; post(`/items/${it.id}`, { status, note }); };
  const timeTask = (t: any, action: string) => { const note = action === "finish" ? window.prompt("Observation (optional):", "") : ""; if (note === null) return; post(`/tasks/${t.id}`, { action, note }); };
  const addLesson = () => { const text = window.prompt("Lesson learned:", ""); if (!text) return; post("/lessons", { text }); };
  const complete = (verdict: string) => { const note = window.prompt(verdict === "GO" ? "GO: note for the record" : "NO_GO: what blocks the cutover?", ""); if (note === null) return; post("/complete", { verdict, note }); };
  const abort = () => { const note = window.prompt("Abort the rehearsal: why?", ""); if (note === null) return; post("/abort", { note }); };
  const loadReport = () => call(() => api(`/cutover/rehearsals/${rid}/report`), (x) => setReport(x.markdown));
  if (!projectId) return <Banner>Select a project.</Banner>;
  if (!manifests.data?.length) return <Banner>Create a manifest first.</Banner>;
  const r = rb.data;
  const active = reh && (reh.status === "PLANNED" || reh.status === "IN_PROGRESS");
  const s = reh?.summary || {};
  const timings = reh?.timings || {};
  return (
    <div>
      <Banner kind="warn">Cutover Command Center is <b>PARTIAL</b>: runbook generation, dependency-aware scheduling, critical path, downtime forecast, rollback gates, go/no-go criteria and the <b>cutover rehearsal checklist</b> (mock cutovers, dress rehearsals, go-live checklist with automatic readiness items, manual items, task timings and an approver verdict) are implemented; live execution tracking of the production cutover, incident escalation and resource assignment are planned. Forecasts are template-based unless a rehearsal timed the task; nothing here touches an SAP system.</Banner>
      <div className="row"><Select value={id} onChange={setMid} options={(manifests.data || []).map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} /><button className="secondary" onClick={assess}>Assess cutover risk (agent)</button></div>
      <ErrorBox error={err || rb.error} />
      {r && <>
        <div className="stats"><Stat label="Total plan" value={`${(r.total_minutes / 60).toFixed(1)} h`} /><Stat label="Forecast downtime" value={`${(r.forecast_downtime_minutes / 60).toFixed(1)} h`} sub="critical-path downtime tasks" /><Stat label="Point of no return" value={r.point_of_no_return} /><Stat label="Critical path" value={r.critical_path.join(" → ")} />{risk && <Stat label="Risk" value={<Pill value={risk.proposal.band} />} sub={`score ${risk.proposal.score}`} />}</div>
        <Card title="Runbook"><Table cols={[{ k: "id", h: "#" }, { k: "name", h: "Task" }, { k: "phase", h: "Phase" }, { k: "depends_on", h: "Depends", r: (x) => x.depends_on.join(", ") }, { k: "est_minutes", h: "Est. min", r: (x) => <span title={`basis: ${x.basis}`}>{x.est_minutes}{x.basis && x.basis.startsWith("rehearsal") ? " ⏱" : ""}</span> }, { k: "earliest_start_min", h: "Start" }, { k: "earliest_finish_min", h: "Finish" }, { k: "downtime", h: "Downtime", r: (x) => (x.downtime ? "yes" : "") }, { k: "irreversible", h: "Irreversible", r: (x) => (x.irreversible ? <Pill value="IRREVERSIBLE" /> : "") }, { k: "owner", h: "Owner" }, { k: "sign_off", h: "Sign-off" }, { k: "cp", h: "Critical", r: (x) => (r.critical_path.includes(x.id) ? "●" : "") }]} rows={r.tasks} /><p className="muted">{r.basis}{r.rehearsal_timed_tasks?.length ? ` Tasks marked ⏱ use minutes measured in a rehearsal: ${r.rehearsal_timed_tasks.join(", ")}.` : ""}</p></Card>
        <div className="grid2"><Card title="Rollback decision gates"><KV obj={r.rollback} /></Card><Card title="Go / no-go criteria">{risk ? <Table cols={[{ k: "criterion", h: "Criterion" }, { k: "met", h: "Met", r: (x) => <Pill value={x.met ? "PASS" : "FAIL"} /> }, { k: "note", h: "Note" }]} rows={risk.proposal.go_no_go_criteria} /> : <div className="muted">Run the risk assessment.</div>}{risk && <KV obj={risk.proposal.factors} />}</Card></div>
        <Card title="Cutover rehearsal checklist" actions={<div className="row"><Select value={kind} onChange={setKind} options={[{ value: "MOCK", label: "mock cutover" }, { value: "DRESS", label: "dress rehearsal" }, { value: "FINAL", label: "go-live checklist" }]} /><button onClick={createRehearsal}>New rehearsal</button></div>}>
          <p className="muted">Every rehearsal starts from the same checklist: <b>automatic</b> items the platform evaluates from its own state (approvals, completed run, reconciliation, sign-offs, final delta, cockpit rounds, evidence, risk, completeness, target) and <b>manual</b> items someone ticks by hand with a note (change freeze, interfaces, batch jobs, backups, number ranges, users, communication, rollback test, hypercare, residual data). Start it, time the runbook tasks as they happen, record lessons, and let an approver close it with GO or NO_GO; GO is refused while a blocking item is open. The automatic items describe the platform's simulated runtime only.</p>
          {(rehearsals.data || []).length > 0 && <Table cols={[{ k: "sequence", h: "#" }, { k: "name", h: "Rehearsal" }, { k: "kind", h: "Kind" }, { k: "status", h: "Status", r: (x) => <Pill value={x.status} /> }, { k: "verdict", h: "Verdict", r: (x) => x.verdict ? <Pill value={x.verdict} /> : "-" }, { k: "summary", h: "Items", r: (x) => `${x.summary?.pass ?? 0} pass · ${x.summary?.fail ?? 0} fail · ${x.summary?.not_applicable ?? 0} n/a · ${x.summary?.pending ?? 0} pending` }, { k: "blocking", h: "Blocking open", r: (x) => (x.summary?.blocking_open || []).join(", ") || "none" }, { k: "timed", h: "Timed", r: (x) => x.summary?.tasks_timed ? `${x.summary.tasks_timed} task(s), ${x.summary.actual_minutes} min` : "-" }, { k: "completed", h: "Closed", r: (x) => x.completed_at ? `${String(x.completed_at).slice(0, 16)} ${x.completed_by}` : "-" }, { k: "x", h: "", r: (x) => <button className="secondary" onClick={() => { setRid(x.id); setReport(null); }}>{x.id === rid ? "Selected" : "Open"}</button> }]} rows={rehearsals.data} />}
          {!rehearsals.data?.length && <p className="muted">No rehearsal yet for this manifest.</p>}
          {reh && <>
            <h4>Rehearsal {reh.sequence}: {reh.name} <Pill value={reh.kind} /> <Pill value={reh.status} /> {reh.verdict && <Pill value={reh.verdict} />}</h4>
            <div className="stats"><Stat label="Blocking open" value={s.blocking_open?.length ?? 0} sub={(s.blocking_open || []).join(", ") || "ready for GO"} /><Stat label="Pass / fail / n/a / pending" value={`${s.pass ?? 0} / ${s.fail ?? 0} / ${s.not_applicable ?? 0} / ${s.pending ?? 0}`} /><Stat label="Tasks timed" value={s.tasks_timed ?? 0} sub={s.tasks_timed ? `${s.actual_minutes} min actual vs ${s.estimated_minutes_of_timed} min estimated` : ""} /><Stat label="Downtime measured" value={`${s.downtime_actual_minutes ?? 0} min`} /></div>
            <div className="row">
              {active && <button className="secondary" onClick={() => post("/refresh")}>Refresh automatic checks</button>}
              {reh.status === "PLANNED" && <button className="secondary" onClick={() => { const note = window.prompt("Start the rehearsal: note (optional)", ""); if (note !== null) post("/start", { note }); }}>Start rehearsal</button>}
              {active && <button className="secondary" onClick={addLesson}>Add lesson</button>}
              {active && <button onClick={() => complete("GO")} disabled={!s.ready_for_go} title={s.ready_for_go ? "" : "blocking items still open"}>Complete: GO</button>}
              {active && <button className="danger" onClick={() => complete("NO_GO")}>Complete: NO-GO</button>}
              {active && <button className="secondary" onClick={abort}>Abort</button>}
              <button className="secondary" onClick={loadReport}>Report (Markdown)</button>
            </div>
            <Table cols={[{ k: "id", h: "#" }, { k: "phase", h: "Phase" }, { k: "title", h: "Item" }, { k: "kind", h: "Kind", r: (it) => <Pill value={it.kind} /> }, { k: "blocking", h: "Blocking", r: (it) => (it.blocking ? "yes" : "") }, { k: "owner", h: "Owner" }, { k: "runbook_task", h: "Task" }, { k: "status", h: "Status", r: (it) => <Pill value={it.status} /> }, { k: "detail", h: "Detail", r: (it) => <span title={JSON.stringify(it.evidence || {})}>{it.detail || it.note || ""}</span> }, { k: "checked_by", h: "By", r: (it) => it.checked_by ? `${it.checked_by} ${String(it.checked_at || "").slice(0, 16)}` : "" }, { k: "x", h: "", r: (it) => active ? (it.kind === "MANUAL" ? <>{it.status !== "PASS" && <button className="secondary" onClick={() => markItem(it, "PASS")}>Pass</button>} {it.status !== "FAIL" && <button className="secondary" onClick={() => markItem(it, "FAIL")}>Fail</button>} {it.status !== "NOT_APPLICABLE" && <button className="secondary" onClick={() => markItem(it, "NOT_APPLICABLE")}>N/A</button>} {it.status !== "PENDING" && <button className="secondary" onClick={() => markItem(it, "PENDING")}>Reset</button>}</> : (it.waived ? <button className="secondary" onClick={() => markItem(it, "PENDING")}>Un-waive</button> : <button className="secondary" onClick={() => markItem(it, "NOT_APPLICABLE")}>Waive</button>)) : "" }]} rows={reh.items} keyFn={(it) => it.id} />
            <h4>Task timings</h4>
            <p className="muted">Record when a runbook task starts and finishes during the rehearsal; a completed rehearsal's minutes replace the template estimate in the runbook above.</p>
            <Table cols={[{ k: "id", h: "#" }, { k: "name", h: "Task" }, { k: "phase", h: "Phase" }, { k: "est_minutes", h: "Est. min" }, { k: "actual", h: "Actual min", r: (t) => timings[t.id]?.actual_minutes ?? (timings[t.id]?.started_at ? "running" : "-") }, { k: "started", h: "Started", r: (t) => String(timings[t.id]?.started_at || "").slice(0, 16) }, { k: "finished", h: "Finished", r: (t) => String(timings[t.id]?.finished_at || "").slice(0, 16) }, { k: "note", h: "Note", r: (t) => timings[t.id]?.note || "" }, { k: "x", h: "", r: (t) => reh.status === "IN_PROGRESS" ? (!timings[t.id]?.started_at || timings[t.id]?.finished_at ? <button className="secondary" onClick={() => timeTask(t, "start")}>{timings[t.id]?.finished_at ? "Restart" : "Start"}</button> : <button className="secondary" onClick={() => timeTask(t, "finish")}>Finish</button>) : "" }]} rows={reh.runbook} keyFn={(t) => t.id} />
            {(reh.lessons || []).length > 0 && <><h4>Lessons</h4><ul>{reh.lessons.map((l: any, i: number) => <li key={i}>{l.text} <span className="muted">({l.by}, {String(l.at).slice(0, 16)}{l.task ? `, task ${l.task}` : ""})</span></li>)}</ul></>}
            {reh.completion_note && <p className="muted">Completion note: {reh.completion_note}</p>}
            {report && <Pre value={report} />}
          </>}
        </Card>
      </>}
    </div>
  );
}

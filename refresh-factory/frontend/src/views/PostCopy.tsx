import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const LABEL: Record<string, string> = { basis_lead: "Basis lead", integration_owner: "Integration owner", security_officer: "Security officer" };
const taskKind = (s: string) => (["DONE", "ALREADY_COMPLIANT"].includes(s) ? "pass" : s === "NOT_APPLICABLE" || s === "PENDING" ? "info" : s === "ROLLED_BACK" ? "warn" : "fail");

export default function PostCopy() {
  const app = useApp();
  const [target, setTarget] = useState(""); const [source, setSource] = useState("");
  const [assess, setAssess] = useState<J>(null); const [profiles, setProfiles] = useState<J[]>([]); const [runs, setRuns] = useState<J[]>([]);
  const [lib, setLib] = useState<J[]>([]); const [profile, setProfile] = useState(""); const [mode, setMode] = useState("deactivate");
  const [open, setOpen] = useState<string | null>(null);
  const act = useAction();
  const targets = app.systems.filter((s) => s.role !== "PRD" && s.writable_target);
  const prds = app.systems.filter((s) => s.role === "PRD");
  const run = runs.find((r) => r.target_id === target);
  const mine = profiles.filter((p) => p.system_id === target);
  const approved = mine.filter((p) => p.status === "APPROVED");

  const load = useCallback(async () => {
    setLib(await api.get("/api/postcopy/tasks")); setProfiles(await api.get("/api/postcopy/profiles")); setRuns(await api.get("/api/postcopy/runs"));
    if (target) setAssess(await api.get(`/api/postcopy/systems/${target}/assessment${profile ? `?profile_id=${profile}` : ""}`));
  }, [target, profile]);
  useEffect(() => { if (!target && targets[0]) setTarget(targets[0].id); if (!source && prds[0]) setSource(prds[0].id); }, [targets.length, prds.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); });
  const rp = (path: string, body?: unknown) => go(() => api.post(`/api/postcopy/runs/${run.id}/${path}`, body ?? {}));

  return (
    <>
      <Card title="Target system" actions={<div className="row">
        <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target system">{targets.map((s) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select>
        <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="Copied from">{prds.map((s) => <option key={s.id} value={s.id}>from {s.label}</option>)}</select></div>}>
        <p className="muted">Post-copy automation only ever runs on a non-production system that is not locked. Everything here acts on a <strong>simulated</strong> technical configuration (RFC, jobs, mail, IDoc, TMS, licence, certificates, users, endpoints, parameters).</p>
        <div className="row"><button disabled={!app.can("system:write") || !target || !source} onClick={() => go(async () => { await api.post(`/api/demo/simulate-system-copy?source_id=${source}&target_id=${target}`); })}>Demo: simulate a system copy into this target</button></div>
        <ErrorNote error={act.error} />
      </Card>

      {assess && (
        <Card title="1 · Assessment: how much of production arrived">
          <div className="grid stats">
            <Stat label="Active production references" value={<span style={{ color: assess.active_production_references ? "var(--bad)" : "var(--ok)" }}>{assess.active_production_references}</span>} />
            <Stat label="Logical system" value={<span className="small mono">{assess.logical_system}</span>} />
            <Stat label="Licence" value={assess.licence_valid ? "valid" : "invalid"} /><Stat label="TMS" value={assess.tms_consistent ? "consistent" : "inconsistent"} />
            <Stat label="Unlocked SAP_ALL users" value={assess.privileged_users.length} hint={assess.privileged_users.join(", ")} />
          </div>
          <DataTable pageSize={7} rows={Object.entries(assess.categories).map(([k, v]: [string, J]) => ({ category: k, ...v }))} cols={[
            { key: "category", title: "Category" }, { key: "items", title: "Items" }, { key: "production_refs", title: "Point to production" },
            { key: "active_production_refs", title: "Active", render: (r) => <StatusBadge s={r.active_production_refs ? "fail" : "pass"} /> },
            { key: "examples", title: "Examples", render: (r) => (r.examples ?? []).join(", ") }]} />
          {assess.tasks_needed && <p className="muted small">{assess.tasks_needed.length} task(s) would change this target against the selected profile.</p>}
        </Card>)}

      <Card title="2 · Target profile (capture BEFORE the copy)">
        <p className="muted small">The target's own technical configuration, captured while it is still clean, approved by someone other than its author. Post-copy restores from it and never from production; it cannot contain production references.</p>
        <div className="row">
          <button className="primary" disabled={!target || !app.can("plan:write") || act.busy} onClick={() => go(async () => { const p = await api.post("/api/postcopy/profiles", { system_id: target, name: `Pre-copy profile ${new Date().toISOString().slice(0, 10)}` }); setProfile(p.id); })}>Capture profile now</button>
          <select value={profile} onChange={(e) => setProfile(e.target.value)} aria-label="Profile"><option value="">no profile selected</option>{mine.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.status}</option>)}</select></div>
        <DataTable rows={mine} pageSize={4} empty="No profile for this target yet" cols={[
          { key: "name", title: "Profile" }, { key: "status", title: "Status", render: (p) => <StatusBadge s={p.status === "APPROVED" ? "pass" : "warn"} /> },
          { key: "captured_by", title: "Captured by" }, { key: "approval", title: "Approved by", render: (p) => p.approval?.by ?? "—" },
          { key: "act", title: "", render: (p) => <div className="row">
            {p.status === "DRAFT" && <button disabled={!app.can("plan:submit")} onClick={() => go(() => api.post(`/api/postcopy/profiles/${p.id}/submit`))}>Submit</button>}
            {p.status === "PENDING_APPROVAL" && <button className="primary" disabled={!app.can("plan:approve")} onClick={() => go(() => api.post(`/api/postcopy/profiles/${p.id}/approve`))}>Approve</button>}</div> }]} />
      </Card>

      <Card title="3 · Run">
        <div className="row">
          <select value={profile} onChange={(e) => setProfile(e.target.value)} aria-label="Approved profile for the run"><option value="">choose approved profile…</option>{approved.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
          <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Neutralisation mode"><option value="deactivate">deactivate production endpoints</option><option value="delete">delete production endpoints</option></select>
          <button className="primary" disabled={!profile || !approved.some((p) => p.id === profile) || !app.can("plan:write") || act.busy} onClick={() => go(() => api.post("/api/postcopy/runs", { target_id: target, source_id: source, profile_id: profile, mode }))}>Plan run</button></div>
        {run && (
          <div className="tpl">
            <p>Run <code>{run.id}</code> <StatusBadge s={run.status === "COMPLETED" ? "pass" : run.status === "READY" || run.status === "AWAITING_APPROVAL" ? "warn" : run.status === "ROLLED_BACK" ? "info" : "fail"} /> {run.status}
              {run.halted_reason && <span className="small"> · {run.halted_reason}</span>}</p>
            {run.required_approvals.length > 0 && (
              <div className="row">{run.required_approvals.map((l: string) => run.approvals[l]
                ? <Badge key={l} kind="ok">{LABEL[l]} ✓ {run.approvals[l].by}</Badge>
                : <button key={l} disabled={!app.can(`postcopy:approve:${l}`)} onClick={() => rp("approvals", { label: l })} title={app.can(`postcopy:approve:${l}`) ? "" : `needs the ${LABEL[l]} role`}>Approve as {LABEL[l]}</button>)}</div>)}
            <div className="row">
              <button className="primary" disabled={!["READY", "HALTED"].includes(run.status) || !app.can("run:execute") || act.busy} onClick={() => rp(run.status === "HALTED" ? "resume" : "execute")}>{run.status === "HALTED" ? "Resume" : "Execute"}</button>
              <button className="danger" disabled={!["COMPLETED", "HALTED"].includes(run.status) || !app.can("postcopy:approve:basis_lead")} onClick={() => confirm("Restore every changed task to its pre-run state?") && rp("rollback")}>Roll back</button>
              <button disabled={!app.can("audit:read")} onClick={() => api.download(`/api/postcopy/runs/${run.id}/evidence`, `${run.id}-evidence.zip`)}>Evidence package</button></div>
            <DataTable rows={run.tasks} pageSize={20} cols={[
              { key: "id", title: "Task" }, { key: "name", title: "What" }, { key: "approval", title: "Approval", render: (t) => (t.approval === "none" ? "—" : LABEL[t.approval]) },
              { key: "status", title: "Status", render: (t) => <><StatusBadge s={taskKind(t.status)} /> {t.status}</> },
              { key: "detail", title: "Detail", render: (t) => t.reason ?? (t.changes != null ? `${t.changes} change(s)` : t.needs_action === false ? "compliant" : "") },
              { key: "ev", title: "", render: (t) => <button onClick={() => setOpen(open === t.id ? null : t.id)}>Evidence</button> }]} />
            {open && <EvidenceBox id={open} runId={run.id} />}
          </div>)}
      </Card>

      {run?.gate && (
        <Card title="4 · Verification gate">
          <p>{run.gate.ok ? <Badge kind="ok">PASSED: nothing active points to production and the target's own configuration is back</Badge> : <Badge kind="bad">FAILED</Badge>}</p>
          <DataTable rows={run.gate.checks} pageSize={8} cols={[{ key: "id", title: "Check" }, { key: "name", title: "What is verified" }, { key: "status", title: "Result", render: (c) => <StatusBadge s={c.status === "pass" ? "pass" : "fail"} /> }, { key: "samples", title: "Examples", render: (c) => c.samples.join(", ") }]} />
        </Card>)}

      <Card title="Task library">
        <DataTable rows={lib} pageSize={6} cols={[{ key: "id", title: "ID" }, { key: "name", title: "Task" }, { key: "area", title: "Area" },
          { key: "versions", title: "Applies to", render: (t) => t.versions.join(", ") }, { key: "approval", title: "Approval" },
          { key: "depends_on", title: "After", render: (t) => t.depends_on.join(", ") || "—" }, { key: "rollback", title: "Rollback / recovery" }]} />
      </Card>
    </>
  );
}

function EvidenceBox({ id, runId }: { id: string; runId: string }) {
  const [run, setRun] = useState<J>(null);
  useEffect(() => { api.get(`/api/postcopy/runs/${runId}`).then(setRun).catch(() => undefined); }, [runId, id]);
  const t = run?.tasks.find((x: J) => x.id === id);
  return <div className="tpl small"><strong>{id}</strong> {t?.name} · status {t?.status}{t?.reason ? ` · ${t.reason}` : ""}<br />
    <span className="muted">Before/after snapshots and the change list are in the evidence package (tasks/{id}.json). Credentials are never recorded.</span></div>;
}

import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, StatusBadge } from "../components";
import { useApp } from "../ctx";

export function Readiness() {
  const { systems } = useApp();
  const [rows, setRows] = useState<J[]>([]);
  useEffect(() => { Promise.all(systems.map((s) => api.get(`/api/systems/${s.id}/readiness`))).then(setRows).catch(() => undefined); }, [systems]);
  return (
    <Card title="Source and target readiness">
      {rows.map((r) => (
        <div key={r.system} className="ready-row"><strong>{r.system}</strong> <StatusBadge s={r.ready ? "pass" : "fail"} />
          <ul className="checks">{r.checks.map((c: J) => <li key={c.id}>{c.ok ? "✓" : "✗"} {c.name}</li>)}</ul></div>))}
      {!rows.length && <p className="muted">Register systems from the Control tower first.</p>}
    </Card>
  );
}

export function FullRefresh() {
  const { project } = useApp();
  const [fr, setFr] = useState<J>(null);
  useEffect(() => { setFr(null); if (project) api.get(`/api/full-refresh/plan?source_id=${project.source.id}&target_id=${project.target.id}`).then(setFr).catch(() => undefined); }, [project?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Card title="Full system refresh runbook">
      <p><Badge kind="warn">design only</Badge> <span className="muted">No execution engine exists. Real copies need SWPM / HANA backup-recovery / snapshot adapters and Basis approval.</span></p>
      {!fr ? <p className="muted">Select a project to evaluate its source/target pair.</p> : (
        <>
          {fr.validation.blockers.map((b: string) => <p key={b}><StatusBadge s="fail" /> {b}</p>)}
          {fr.validation.warnings.map((b: string) => <p key={b}><StatusBadge s="warn" /> {b}</p>)}
          <ol className="phases">{fr.phases.map((p: J) => (
            <li key={p.no}><strong>{p.name}</strong> <span className="muted">{p.performed_by}{p.approval_required ? ` · approval: ${p.rollback === "none" ? "required" : "required"}` : ""} · rollback: {p.rollback}</span> <Badge>{p.status}</Badge></li>))}</ol>
        </>)}
    </Card>
  );
}

export function PostCopy() {
  const [tasks, setTasks] = useState<J[]>([]);
  useEffect(() => { api.get("/api/post-copy/tasks").then(setTasks).catch(() => undefined); }, []);
  return (
    <Card title="Post-copy automation catalog">
      <p><Badge kind="warn">catalogued — not executable</Badge> <span className="muted">Each task defines prerequisites, checks, rollback, evidence and approval. Production interfaces are never re-activated.</span></p>
      <DataTable rows={tasks} cols={[
        { key: "id", title: "ID" }, { key: "name", title: "Task" }, { key: "area", title: "Area" }, { key: "precheck", title: "Pre-check" },
        { key: "action", title: "Action" }, { key: "postcheck", title: "Post-check" }, { key: "rollback", title: "Rollback" }, { key: "approval", title: "Approval" }]} pageSize={6} />
    </Card>
  );
}

export function TestCatalog() {
  return (
    <Card title="Test data catalog">
      <p><Badge kind="bad">planned</Badge></p>
      <p className="muted">Scenario templates (Order-to-Cash, Procure-to-Pay, …), reservation, expiry and CI/CD hooks are not implemented yet. The selective designer already produces the business-complete slices these templates will reuse.</p>
    </Card>
  );
}

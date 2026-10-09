import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, StatusBadge } from "../components";
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

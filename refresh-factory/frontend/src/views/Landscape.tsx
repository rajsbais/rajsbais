import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

export default function Landscape() {
  const { systems, project } = useApp();
  const [sel, setSel] = useState<string | null>(null);
  const [disc, setDisc] = useState<J>(null);
  const [ready, setReady] = useState<J>(null);
  const [combos, setCombos] = useState<J[]>([]);
  const [fr, setFr] = useState<J>(null);
  const act = useAction();
  useEffect(() => { if (systems.length) api.get("/api/landscape/combinations").then(setCombos).catch(() => undefined); }, [systems]);
  useEffect(() => {
    if (!sel) return;
    void act.run(async () => { setDisc(await api.get(`/api/systems/${sel}/discovery`)); setReady(await api.get(`/api/systems/${sel}/readiness`)); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);
  const roles = ["PRD", "QAS", "UAT", "DEV", "SBX", "TRN"];
  return (
    <>
      <Card title="Environments">
        <div className="env-row">
          {roles.map((r) => (
            <div key={r} className="env-col"><h4>{r}</h4>
              {systems.filter((s) => s.role === r).map((s) => (
                <button key={s.id} className={`env ${sel === s.id ? "active" : ""} ${r === "PRD" ? "prd" : ""}`} onClick={() => setSel(s.id)}>
                  <strong>{s.sid}/{s.client}</strong><span>{s.product}</span><Badge>{s.family}</Badge>
                  {s.writable_target ? <Badge kind="ok">writable target</Badge> : <Badge kind="bad">read-only</Badge>}
                </button>))}
              {!systems.some((s) => s.role === r) && <span className="muted small">none</span>}
            </div>))}
        </div>
      </Card>
      <ErrorNote error={act.error} />
      {disc && (
        <div className="grid two">
          <Card title={`Discovery — ${disc.system.sid}/${disc.system.client}`}>
            <dl className="kv">
              <dt>Product</dt><dd>{disc.system.product}</dd><dt>Database</dt><dd>{disc.system.db_type} {disc.system.db_version}</dd>
              <dt>Components</dt><dd>{disc.installed_components.map((c: J) => `${c.name} ${c.release}`).join(", ")}</dd>
              <dt>Custom fields</dt><dd>{disc.custom_fields.join(", ") || "—"}</dd>
              <dt>Company codes</dt><dd>{disc.company_codes.map((c: J) => `${c.code} ${c.name}`).join("; ")}</dd>
              <dt>Plants</dt><dd>{disc.plants.map((p: J) => `${p.plant} (${p.company_code})`).join(", ")}</dd>
            </dl>
            <div className="chips">{Object.entries(disc.business_objects).map(([k, v]) => <Badge key={k}>{k.replace("_", " ")}: {String(v)}</Badge>)}</div>
          </Card>
          <Card title="Readiness assessment">
            {ready && <><p>{ready.ready ? <Badge kind="ok">ready</Badge> : <Badge kind="bad">not ready</Badge>}</p>
              <ul className="checks">{ready.checks.map((c: J) => <li key={c.id}>{c.ok ? "✓" : "✗"} {c.name}</li>)}</ul></>}
          </Card>
        </div>)}
      <Card title="Supported refresh combinations">
        <DataTable rows={combos} cols={[
          { key: "label", title: "Route" },
          { key: "selective", title: "Selective", render: (c) => <StatusBadge s={c.selective ? "pass" : "fail"} /> },
          { key: "full_system_refresh", title: "Full refresh", render: (c) => <StatusBadge s={c.full_system_refresh ? "pass" : "fail"} /> },
          { key: "blockers", title: "Blockers", render: (c) => c.blockers.join("; ") || "—" },
          { key: "plan", title: "", render: (c) => <button onClick={() => act.run(async () => setFr(await api.get(`/api/full-refresh/plan?source_id=${c.source}&target_id=${c.target}`)))}>Runbook</button> },
        ]} />
      </Card>
      {fr && (
        <Card title="Full system refresh runbook (design only — no execution engine)">
          <Badge kind="warn">not executable</Badge>
          <ol className="phases">{fr.phases.map((p: J) => (
            <li key={p.no}><strong>{p.name}</strong> <span className="muted">{p.performed_by}{p.approval_required ? " · approval" : ""}</span> <StatusBadge s={p.status === "blocked" ? "fail" : "info"} /></li>))}</ol>
        </Card>)}
      {project && <p className="muted">Active project route: {project.source.label ?? project.source.sid} → {project.target.label ?? project.target.sid}</p>}
    </>
  );
}

import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

export default function Landscape() {
  const { systems, project, can, reload } = useApp();
  const [remote, setRemote] = useState<J>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [disc, setDisc] = useState<J>(null);
  const [ready, setReady] = useState<J>(null);
  const [combos, setCombos] = useState<J[]>([]);
  const [fr, setFr] = useState<J>(null);
  const act = useAction();
  const [f, setF] = useState({ kind: "rfc", sid: "", role: "SBX", client: "100", host: "", sysnr: "00", user: "", pwEnv: "", cd: false, maxRows: 500, cpm: 120, confirm: false });
  const [smoke, setSmoke] = useState<J>(null);
  const profileOf = () => ({
    system: { sid: f.sid.toUpperCase(), client: f.client, role: f.role, owner: "onboarding" },
    profile: f.kind === "rfc"
      ? { name: `${f.sid} via RFC`, kind: "rfc", ashost: f.host, sysnr: f.sysnr, client: f.client, user: f.user, password_ref: `env:${f.pwEnv}`, calls_per_minute: f.cpm, options: f.cd ? { change_documents: true } : {} }
      : { name: `${f.sid} via OData`, kind: "odata", base_url: f.host, client: f.client, user: f.user, password_ref: `env:${f.pwEnv}`, calls_per_minute: f.cpm, options: {} },
  });
  const formOk = !!(f.sid && f.host && f.user && f.pwEnv);
  useEffect(() => { if (systems.length) api.get("/api/landscape/combinations").then(setCombos).catch(() => undefined); }, [systems]);
  useEffect(() => {
    if (!sel) return;
    void act.run(async () => {
      setDisc(await api.get(`/api/systems/${sel}/discovery`)); setReady(await api.get(`/api/systems/${sel}/readiness`));
      setRemote(systems.find((s) => s.id === sel)?.adapter && ["rfc", "odata"].includes(systems.find((s) => s.id === sel)?.adapter) ? await api.get(`/api/systems/${sel}/remote`) : null);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);
  const roles = ["PRD", "QAS", "UAT", "DEV", "SBX", "TRN"];
  return (
    <>
      <Card title="Environments" actions={<div className="row"><button disabled={!can("system:write") || act.busy} title="Registers a second ECC production source reached through the RFC adapter over a FAKE transport"
        onClick={() => act.run(async () => { const r = await api.post("/api/demo/connect-fake-rfc"); await reload(); setSel(r.id); })}>Connect demo remote source (fake RFC)</button>
        <button disabled={!can("system:write") || act.busy} title="Same, with the change-document (CDHDR) reader switched on"
        onClick={() => act.run(async () => { const r = await api.post("/api/demo/connect-fake-rfc?change_documents=true"); await reload(); setSel(r.id); })}>Demo remote source with change documents</button>
        <button disabled={!can("system:write") || act.busy} title="An S/4HANA-style source reached through released OData APIs over a FAKE endpoint"
        onClick={() => act.run(async () => { const r = await api.post("/api/demo/connect-fake-odata"); await reload(); setSel(r.id); })}>Connect demo OData source (fake)</button></div>}>
        <div className="env-row">
          {roles.map((r) => (
            <div key={r} className="env-col"><h3>{r}</h3>
              {systems.filter((s) => s.role === r).map((s) => (
                <button key={s.id} className={`env ${sel === s.id ? "active" : ""} ${r === "PRD" ? "prd" : ""}`} onClick={() => setSel(s.id)}>
                  <strong>{s.sid}/{s.client}</strong><span>{s.product}</span><Badge>{s.family}</Badge>
                  {s.writable_target ? <Badge kind="ok">writable target</Badge> : <Badge kind="bad">read-only</Badge>}
                  {s.adapter === "rfc" && <Badge kind="info">remote (RFC)</Badge>}
                  {s.adapter === "odata" && <Badge kind="info">remote (OData)</Badge>}
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
            {disc.business_objects ? <div className="chips">{Object.entries(disc.business_objects).map(([k, v]) => <Badge key={k}>{k.replace("_", " ")}: {String(v)}</Badge>)}</div>
              : <p className="muted small">{disc.note}</p>}
          </Card>
          <Card title="Readiness assessment">
            {ready && <><p>{ready.ready ? <Badge kind="ok">ready</Badge> : <Badge kind="bad">not ready</Badge>}</p>
              <ul className="checks">{ready.checks.map((c: J) => <li key={c.id}>{c.ok ? "✓" : "✗"} {c.name}</li>)}</ul></>}
          </Card>
        </div>)}
      {can("system:write") && (
        <Card title="Connect a real system (read-only)">
          <p className="muted small">Test first, then register. Use a sandbox or a copy and a dedicated read-only SAP user. The password is never typed here: give the <strong>name</strong> of an environment variable on the machine that runs this API.
            The test reads at most the number of rows below per table, never writes, and its report contains no row values. A registered system is a read-only source only.</p>
          <div className="form-grid">
            <label>Connection type<select value={f.kind} onChange={(e) => { setF({ ...f, kind: e.target.value }); setSmoke(null); }}><option value="rfc">RFC (ECC or S/4HANA)</option><option value="odata">OData (S/4HANA)</option></select></label>
            <label>System ID (SID)<input value={f.sid} maxLength={8} onChange={(e) => setF({ ...f, sid: e.target.value })} /></label>
            <label>Role<select value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{roles.map((r) => <option key={r}>{r}</option>)}</select></label>
            <label>Client<input value={f.client} maxLength={3} onChange={(e) => setF({ ...f, client: e.target.value })} /></label>
            <label>{f.kind === "rfc" ? "Application server host" : "Base URL (https)"}<input value={f.host} onChange={(e) => setF({ ...f, host: e.target.value })} /></label>
            {f.kind === "rfc" && <label>Instance number<input value={f.sysnr} maxLength={2} onChange={(e) => setF({ ...f, sysnr: e.target.value })} /></label>}
            <label>SAP user (read-only)<input value={f.user} autoComplete="off" onChange={(e) => setF({ ...f, user: e.target.value })} /></label>
            <label>Password environment variable (name)<input value={f.pwEnv} autoComplete="off" onChange={(e) => setF({ ...f, pwEnv: e.target.value })} /></label>
            <label>Rows read per table (max)<input type="number" min={1} max={5000} value={f.maxRows} onChange={(e) => setF({ ...f, maxRows: Number(e.target.value) })} /></label>
            <label>Calls per minute (throttle)<input type="number" min={1} max={100000} value={f.cpm} onChange={(e) => setF({ ...f, cpm: Number(e.target.value) })} /></label>
            {f.kind === "rfc" && <label className="check"><input type="checkbox" checked={f.cd} onChange={(e) => setF({ ...f, cd: e.target.checked })} />Probe the change-document reader (CDHDR)</label>}
          </div>
          {f.role === "PRD" && <p className="small"><Badge kind="warn">production</Badge> Production can only ever be a read-only source here, and a first test should not be against it.</p>}
          <label className="check"><input type="checkbox" checked={f.confirm} onChange={(e) => setF({ ...f, confirm: e.target.checked })} />This is a sandbox or a copy, and the SAP user is read-only</label>
          <div className="row">
            <button className="primary" disabled={!formOk || !f.confirm || act.busy}
              onClick={() => act.run(async () => setSmoke(await api.post("/api/systems/smoke", { ...profileOf(), max_rows: f.maxRows, confirm: f.confirm })))}>Run smoke test</button>
            <button disabled={!smoke || !smoke.connection?.ok || smoke.verdict.some((v: J) => v.level === "BLOCKER") || act.busy}
              onClick={() => act.run(async () => { const r = await api.post("/api/systems/connect", profileOf()); await reload(); setSel(r.id); setSmoke(null); })}>Register as read-only source</button>
          </div>
          <ErrorNote error={act.error} />
          {smoke && (
            <div aria-live="polite">
              <h3>Smoke test result</h3>
              <ul>{smoke.verdict.map((v: J, i: number) => <li key={i}><Badge kind={v.level === "BLOCKER" ? "bad" : v.level === "ATTENTION" ? "warn" : "ok"}>{v.level}</Badge> {v.text}</li>)}</ul>
              {Object.keys(smoke.drift).length > 0 && <p className="small">Differences from the platform's model: {Object.entries(smoke.drift).map(([t, v]: J) => `${t}: ${v.slice(0, 2).join("; ")}`).join(" | ")}</p>}
              <DataTable rows={Object.entries(smoke.tables).map(([t, e]: J) => ({ table: t, ...e }))} empty="Nothing was read." cols={[
                { key: "table", title: "Table" }, { key: "status", title: "Status", render: (e: J) => <span>{e.status}{e.error_class ? ` (${e.error_class})` : ""}</span> },
                { key: "rows_read", title: "Rows read", render: (e: J) => <span>{e.rows_read ?? ""}{e.capped ? "+" : ""}</span> }, { key: "seconds", title: "Seconds" },
                { key: "note", title: "Note", render: (e: J) => <span className="muted small">{e.note || e.message || ""}</span> }]} />
              <p className="muted small">The report contains no row values. {smoke.stats?.calls ?? 0} calls were made; nothing was written.</p>
            </div>)}
        </Card>)}
      {remote && (
        <Card title="Remote connection (read-only)">
          <p><Badge kind="warn">not validated against a real SAP system</Badge> <span className="muted small">{remote.capabilities.full_scan}; writes: {String(remote.capabilities.writes)}; change documents: {String(remote.capabilities.change_documents)}</span></p>
          {remote.change_documents.enabled
            ? <p className="small">Change documents (CDHDR) are read: covered {remote.change_documents.covered.join(", ") || "nothing"}; {remote.change_documents.not_logged.length ? `not logged by the system (compared by content): ${remote.change_documents.not_logged.join(", ")}; ` : ""}
                types without change documents (BOMs, production orders, material documents) are always compared by content. Reads stop {remote.change_documents.lag_seconds}s behind the system clock and re-read {remote.change_documents.overlap_seconds}s.
                {remote.change_documents.error && <> <Badge kind="warn">{remote.change_documents.error}</Badge></>}</p>
            : <p className="small muted">Change documents: off. {remote.change_documents.note}</p>}
          {remote.profile?.kind === "odata" && <p className="small">OData mapping: {remote.capabilities.mapped_tables.join(", ")}. Tables with fields the API does not supply: {Object.entries(remote.capabilities.tables_with_gaps).map(([t, f]: J) => `${t} (${f.join(", ")})`).join("; ") || "none"}.
            {" "}{remote.capabilities.tables_unavailable.length} other tables are unavailable through this connection, so scopes that need them are blocked in the plan instead of being copied incompletely.</p>}
          <p className="small">Calls {remote.stats.calls} · retries {remote.stats.retries} · rows read {remote.stats.rows} · full scans {remote.stats.scans} ({remote.stats.scanned_tables.join(", ") || "none"}) · guard trips {remote.stats.guard_trips}</p>
          <p className="small">Pushed down to the system: {remote.capabilities.pushdown.join(", ")}.
            {Object.keys(remote.schema_drift).length ? ` Schema drift: ${JSON.stringify(remote.schema_drift)}` : " The modelled DDIC fields and keys all exist remotely."}</p>
        </Card>)}
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

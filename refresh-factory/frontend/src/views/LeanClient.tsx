import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Bars, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const fmt = (n: number) => (n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`);

export default function LeanClient() {
  const app = useApp();
  const [cat, setCat] = useState<J>(null);
  const [presets, setPresets] = useState<J[]>([]);
  const [tpls, setTpls] = useState<J[]>([]);
  const [clients, setClients] = useState<J[]>([]);
  const [builds, setBuilds] = useState<J[]>([]);
  const [est, setEst] = useState<J>(null);
  const [src, setSrc] = useState(""); const [host, setHost] = useState(""); const [tpl, setTpl] = useState("");
  const [client, setClient] = useState("300"); const [name, setName] = useState("");
  const act = useAction();
  const prds = app.systems.filter((s) => s.role === "PRD");
  const hosts = app.systems.filter((s) => s.role !== "PRD" && !s.tags?.includes("lean-client"));
  const approved = tpls.filter((t) => t.status === "APPROVED");
  const selSrc = app.systems.find((s) => s.id === src);

  const load = useCallback(async () => {
    setCat(await api.get("/api/lean/profiles"));
    setTpls(await api.get("/api/lean/templates"));
    setClients(await api.get("/api/lean/clients"));
    setBuilds(await api.get("/api/lean/builds"));
    if (src) setPresets(await api.get(`/api/lean/presets?source_id=${src}`));
  }, [src]);
  useEffect(() => { if (!src && prds[0]) setSrc(prds[0].id); }, [prds.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); await app.reload(); });
  const last = builds[0];

  return (
    <>
      <Card title="Three different workflows">
        <div className="grid two">{cat?.workflows.map((w: J) => (
          <div key={w.id} className={`tpl ${w.id === "client_build" ? "" : "muted"}`}><h3>{w.name}</h3>
            <p className="small"><strong>Scope:</strong> {w.scope}<br /><strong>Copies:</strong> {w.copies}<br /><strong>Typical use:</strong> {w.typical_use}</p>
            <Badge kind={w.implemented === "simulated" ? "warn" : "bad"}>{w.implemented}</Badge></div>))}</div>
        <p className="muted small">A lean client is a new client in an existing non-production system. Client-independent customizing, the repository, user master and authorizations are not modelled here; reserved clients 000, 001, 066 are never touched.</p>
      </Card>

      <Card title="1 · Environment template" actions={<select value={src} onChange={(e) => setSrc(e.target.value)} aria-label="Reference source">
        {prds.map((s) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select>}>
        <p className="muted">Start from a purpose preset; the author reviews it, an approver (not the author) approves it, and then clients can be built from it.</p>
        <div className="grid two">{presets.map((p) => (
          <div key={p.purpose} className="tpl"><h3>{p.name}</h3>
            <p className="small">{p.masters.map((m: J) => `${m.type} ≤${m.max}`).join(" · ")}<br />{p.transactions.length ? p.transactions.map((t: J) => `${t.count}× ${t.template_id} (${t.mode})`).join(" · ") : "no transactional data"}</p>
            <p className="small muted">masking {p.masking_policy_id} · limit {p.max_rows} rows · keep {p.retention_days}d · {p.protect_after_build ? "protected after build" : "stays writable"}</p>
            <button disabled={!app.can("plan:write") || act.busy} onClick={() => go(async () => { const t = await api.post("/api/lean/templates", { name: p.name, source_id: src, purpose: p.purpose, company_codes: p.customizing.company_codes,
              masters: p.masters, transactions: p.transactions, masking_policy_id: p.masking_policy_id, max_rows: p.max_rows, retention_days: p.retention_days, protect_after_build: p.protect_after_build }); setTpl(t.id); })}>Create template</button></div>))}</div>
        <ErrorNote error={act.error} />
        <DataTable rows={tpls} pageSize={5} empty="No templates yet" cols={[
          { key: "name", title: "Template" }, { key: "purpose", title: "Purpose", render: (t) => `${t.purpose} → ${t.role}` },
          { key: "status", title: "Status", render: (t) => <StatusBadge s={t.status === "APPROVED" ? "pass" : t.status === "DRAFT" ? "info" : "warn"} /> },
          { key: "masking", title: "Masking", render: (t) => `${t.masking_policy_id} +${t.masking_rules.length} rule(s)` },
          { key: "act", title: "", render: (t) => <div className="row">
            {t.status === "DRAFT" && <button disabled={!app.can("plan:submit")} onClick={() => go(() => api.post(`/api/lean/templates/${t.id}/submit`))}>Submit</button>}
            {t.status === "PENDING_APPROVAL" && <button className="primary" disabled={!app.can("plan:approve")} onClick={() => go(() => api.post(`/api/lean/templates/${t.id}/approve`))}>Approve</button>}
            <button onClick={() => setTpl(t.id)}>Select</button></div> }]} />
      </Card>

      <Card title="2 · Right-size: lean client vs full client copy">
        <div className="form">
          <label>Template<select aria-label="Environment template" value={tpl} onChange={(e) => setTpl(e.target.value)}><option value="">choose…</option>
            {tpls.map((t) => <option key={t.id} value={t.id}>{t.name} · {t.status}</option>)}</select></label>
          <label>Host system (new client goes here)<select aria-label="Host system" value={host} onChange={(e) => setHost(e.target.value)}><option value="">choose…</option>
            {hosts.filter((h) => !selSrc || h.family === selSrc.family).map((s) => <option key={s.id} value={s.id}>{s.sid} · {s.role} · {s.family}</option>)}</select></label>
        </div>
        <button disabled={!tpl || !host || act.busy} onClick={() => act.run(async () => setEst(await api.post(`/api/lean/templates/${tpl}/estimate`, { host_id: host })))}>Estimate</button>
        {est && (
          <>
            <div className="grid stats">
              <Stat label="Lean client" value={`${est.lean.rows} rows`} hint={fmt(est.lean.bytes)} />
              <Stat label="Full client copy" value={`${est.full_client_copy.rows} rows`} hint={fmt(est.full_client_copy.bytes)} />
              <Stat label="Smaller by" value={`${est.savings.rows_pct}%`} hint={`${est.savings.bytes_pct}% by size`} />
              <Stat label="Est. duration" value={`${est.duration.lean.seconds}s`} hint={`vs ${est.duration.full_client_copy.seconds}s · placeholder model`} />
            </div>
            <div className="grid two"><div><h3>What the lean client contains (rows per table)</h3><Bars data={est.lean.tables} /></div>
              <div><h3>Plan</h3><ul className="checks">{est.lean.specs.map((s: J, i: number) => <li key={i}><Badge>{s.kind}</Badge> {s.type ?? s.template} · {s.objects} object(s)</li>)}</ul>
                {est.customizing_pulled_in_by_data.length > 0 && <p className="small">Customizing pulled in because the data needs it: {est.customizing_pulled_in_by_data.join(", ")}</p>}
                {est.notes.map((n: string) => <p key={n} className="muted small">{n}</p>)}</div></div>
            <p className="muted small">{est.caveats.join(" ")}</p>
          </>)}
      </Card>

      <Card title="3 · Build the client">
        <div className="form">
          <label>New client number<input value={client} onChange={(e) => setClient(e.target.value)} maxLength={3} /></label>
          <label>Name (optional)<input value={name} onChange={(e) => setName(e.target.value)} /></label>
        </div>
        <button className="primary" disabled={!tpl || !host || act.busy || !app.can("client:build") || !approved.some((t) => t.id === tpl)}
                onClick={() => go(async () => { await api.post("/api/lean/builds", { template_id: tpl, host_id: host, client, name: name || null }); })}>Build client</button>
        {!approved.some((t) => t.id === tpl) && tpl && <span className="muted small"> The template must be approved first.</span>}
        {last && (
          <div className="tpl">
            <p>Build <code>{last.id}</code> · client {last.client} <StatusBadge s={last.status === "READY" ? "pass" : last.status === "BLOCKED" ? "warn" : "fail"} /> {last.status}</p>
            <ol className="timeline">{last.phases.map((p: J) => <li key={p.name}><StatusBadge s={p.status === "DONE" ? "pass" : p.status === "BLOCKED" ? "warn" : "fail"} /> <strong>{p.name}</strong> <span className="muted small mono">{JSON.stringify(p.detail)}</span></li>)}</ol>
            {last.reasons.map((r: string) => <p key={r} className="small"><StatusBadge s="fail" /> {r}</p>)}
            {last.checks.length > 0 && <DataTable rows={last.checks} pageSize={8} cols={[{ key: "id", title: "Check" }, { key: "name", title: "What is verified" }, { key: "status", title: "Result", render: (c) => <StatusBadge s={c.status === "pass" ? "pass" : "fail"} /> }, { key: "detail", title: "Detail" }]} />}
          </div>)}
      </Card>

      <Card title="Lean clients">
        <DataTable rows={clients} pageSize={6} empty="No lean clients yet" cols={[
          { key: "label", title: "Client" }, { key: "status", title: "Status", render: (c) => <StatusBadge s={c.status === "READY" ? "pass" : "warn"} /> },
          { key: "rows", title: "Rows", render: (c) => c.actual?.rows }, { key: "locked", title: "Overwrite protection", render: (c) => <Badge kind={c.locked ? "ok" : "warn"}>{c.locked ? "locked" : "writable"}</Badge> },
          { key: "expires_at", title: "Expires", render: (c) => c.expires_at?.slice(0, 10) },
          { key: "act", title: "", render: (c) => <div className="row">
            <button disabled={!app.can("client:build") || c.status === "EXPIRED"} onClick={() => go(() => api.post(`/api/lean/clients/${c.system_id}/protection`, { locked: !c.locked }))}>{c.locked ? "Unlock" : "Lock"}</button>
            <button className="danger" disabled={!app.can("plan:approve")} onClick={() => confirm(`Remove client ${c.label} and everything in it?`) && go(() => api.post(`/api/lean/builds/${c.id}/decommission`))}>Decommission</button></div> }]} />
        <div className="row"><button disabled={!app.can("run:execute")} onClick={() => go(async () => { const r = await api.post("/api/lean/sweep"); alert(`${r.expired.length} client(s) expired and locked`); })}>Run expiry sweep</button></div>
      </Card>
    </>
  );
}

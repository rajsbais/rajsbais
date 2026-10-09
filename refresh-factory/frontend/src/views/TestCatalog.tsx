import { useCallback, useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const stateKind = (s: string) => ({ AVAILABLE: "pass", RESERVED: "info", CONSUMED: "warn", EXPIRED: "fail", RETIRED: "fail", PURGED: "fail" } as Record<string, string>)[s] ?? "info";

export default function TestCatalog() {
  const app = useApp();
  const [target, setTarget] = useState("");
  const [tpl, setTpl] = useState<J>({ available: [], planned: [] });
  const [policies, setPolicies] = useState<J[]>([]);
  const [cat, setCat] = useState<J[]>([]);
  const [reqs, setReqs] = useState<J[]>([]);
  const [result, setResult] = useState<J>(null);
  const [f, setF] = useState({ template_id: "o2c_complete", mode: "auto", count: 1, company_code: "1000", days: 90, ttl_days: 7, purpose: "", tc_id: "", tc_title: "", reserve: true });
  const [flt, setFlt] = useState({ template_id: "", state: "", provenance: "", q: "" });
  const act = useAction();
  const targets = app.systems.filter((s) => s.writable_target);
  const tgt = targets.find((s) => s.id === target);
  const policy = policies.find((p) => p.target_id === target && p.status === "ACTIVE");
  const pending = policies.find((p) => p.target_id === target && ["DRAFT", "PENDING_APPROVAL"].includes(p.status));
  const me = app.me?.id as string;

  const load = useCallback(async () => {
    setTpl(await api.get("/api/tdm/templates"));
    setPolicies(await api.get("/api/tdm/policies"));
    setReqs(await api.get("/api/tdm/requests"));
    const q = new URLSearchParams(); if (target) q.set("target_id", target);
    Object.entries(flt).forEach(([k, v]) => v && q.set(k, v as string));
    setCat(await api.get(`/api/tdm/catalog?${q}`));
  }, [target, flt]);
  useEffect(() => { if (!target && targets[0]) setTarget(targets[0].id); }, [targets.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { act.run(load); }, [load, app.me?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await load(); });
  const d = (path: string, body?: unknown) => go(() => api.post(`/api/tdm/datasets/${path}`, body ?? {}));

  const submit = () => go(async () => {
    const tc = f.tc_id ? [{ system: "manual", id: f.tc_id, title: f.tc_title }] : [];
    setResult(await api.post("/api/tdm/requests", { target_id: target, template_id: f.template_id, mode: f.mode, count: f.count,
      params: { company_code: f.company_code, days: f.days || null }, purpose: f.purpose, test_cases: tc, ttl_days: f.ttl_days, reserve: f.reserve }));
  });

  const rowActions = (r: J) => (
    <div className="row">
      {r.state === "AVAILABLE" && <button disabled={!app.can("tdm:request")} onClick={() => d(`${r.id}/reserve`, { ttl_days: f.ttl_days, reason: f.purpose })}>Reserve</button>}
      {r.state === "RESERVED" && (r.reserved_by === me || app.can("tdm:curate")) && <>
        <button onClick={() => d(`${r.id}/release`, { consumed: false })}>Release</button>
        <button onClick={() => { const id = prompt("Test case id"); if (!id) return; const o = prompt("Outcome: passed / failed / blocked", "passed"); if (o) void d(`${r.id}/usage`, { test_case: { system: "manual", id, title: id }, outcome: o, consumed: confirm("Did the test change the data (mark consumed)?") }); }}>Record usage</button></>}
      <button onClick={() => act.run(async () => { const v = await api.get(`/api/tdm/datasets/${r.id}/verify`); alert(v.intact ? `Intact (${v.rows_checked} rows checked)` : `Drift in ${v.drift_count} row(s)`); })}>Verify</button>
      {app.can("tdm:curate") && r.provenance !== "discovered" && !r.golden && ["AVAILABLE", "RESERVED"].includes(r.state) && <button onClick={() => d(`${r.id}/golden`)}>Make golden</button>}
      {app.can("tdm:curate") && r.golden && <button onClick={() => d(`${r.id}/restore`)}>Restore</button>}
      {app.can("tdm:curate") && ["AVAILABLE", "CONSUMED", "EXPIRED"].includes(r.state) && <button onClick={() => d(`${r.id}/retire`)}>Retire</button>}
      {app.can("plan:approve") && ["EXPIRED", "RETIRED"].includes(r.state) && r.owned_objects > 0 && <button className="danger" onClick={() => confirm("Delete the rows this platform created for this dataset from the target?") && d(`${r.id}/purge`, { force: false })}>Purge</button>}
    </div>);

  return (
    <>
      <div className="grid stats">
        <Stat label="Datasets in catalog" value={cat.length} /><Stat label="Available" value={cat.filter((c) => c.state === "AVAILABLE").length} />
        <Stat label="Reserved" value={cat.filter((c) => c.state === "RESERVED").length} /><Stat label="Golden" value={cat.filter((c) => c.golden).length} />
      </div>
      <Card title="Target and policy" actions={<select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target system">
        {targets.map((s) => <option key={s.id} value={s.id}>{s.label} · {s.family}</option>)}</select>}>
        {policy ? <p><Badge kind="ok">policy active</Badge> {policy.name} · ≤{policy.max_objects_per_request} objects/request · ≤{policy.max_active_reservations_per_user} reservations/user · ≤{policy.max_ttl_days}d reservations · retention {policy.retention_days}d
          · {policy.masking_rules.length} extra masking rule(s) <span className="muted small">approved by {policy.approval?.by}</span></p>
          : <p><Badge kind="bad">no active policy</Badge> <span className="muted">Self-service needs an approved policy for this target.{pending ? ` Policy “${pending.name}” is ${pending.status}.` : ""}</span></p>}
        <div className="row">
          {!policy && !pending && tgt && <button className="primary" disabled={!app.can("plan:write")} onClick={() => go(async () => {
            const src = app.systems.find((s) => s.role === "PRD" && s.family === tgt.family);
            await api.post("/api/tdm/policies", { name: `Self-service ${tgt.sid}`, source_id: src?.id, target_id: tgt.id }); })}>Create default policy</button>}
          {pending?.status === "DRAFT" && <button disabled={!app.can("plan:submit")} onClick={() => go(() => api.post(`/api/tdm/policies/${pending.id}/submit`))}>Submit policy</button>}
          {pending?.status === "PENDING_APPROVAL" && <button className="primary" disabled={!app.can("plan:approve")} onClick={() => go(() => api.post(`/api/tdm/policies/${pending.id}/approve`))}>Approve policy</button>}
          {policy && <button disabled={!app.can("plan:approve")} onClick={() => go(() => api.post(`/api/tdm/policies/${policy.id}/suspend`))}>Suspend</button>}
          <button disabled={!app.can("tdm:curate") || !target} onClick={() => go(async () => { const r = await api.post(`/api/tdm/scan?target_id=${target}`); alert(`Scan added ${r.added} dataset(s); skipped ${r.skipped_tester_owned} tester-owned object(s)`); })}>Scan target for existing data</button>
          <button disabled={!app.can("run:execute")} onClick={() => go(async () => { const r = await api.post("/api/tdm/sweep"); alert(`Released ${r.reservations_released.length} reservation(s), expired ${r.datasets_expired.length} dataset(s)`); })}>Run lifecycle sweep</button>
        </div>
        <p className="muted small">Policy changes need an approver who is not the author. AI agents can request data but cannot approve, curate or purge.</p>
        <ErrorNote error={act.error} />
      </Card>

      <Card title="Request test data">
        <div className="form">
          <label>Business scenario<select aria-label="Business scenario" value={f.template_id} onChange={(e) => setF({ ...f, template_id: e.target.value })}>
            {tpl.available.map((t: J) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label>
          <label>How<select aria-label="Provisioning mode" value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>
            <option value="auto">Automatic (catalog → subset → synthetic)</option><option value="catalog">Reuse from catalog</option>
            <option value="subset">Subset of masked source data</option><option value="synthetic">Synthetic</option></select></label>
          <label>How many<input type="number" min={1} value={f.count} onChange={(e) => setF({ ...f, count: Number(e.target.value) })} /></label>
          <label>Company code<input value={f.company_code} onChange={(e) => setF({ ...f, company_code: e.target.value })} /></label>
          <label>Source documents of the last N days<input type="number" min={0} value={f.days} onChange={(e) => setF({ ...f, days: Number(e.target.value) })} /></label>
          <label>Reserve for (days)<input type="number" min={1} value={f.ttl_days} onChange={(e) => setF({ ...f, ttl_days: Number(e.target.value) })} /></label>
          <label>Purpose<input value={f.purpose} onChange={(e) => setF({ ...f, purpose: e.target.value })} placeholder="e.g. billing regression CHG-1234" /></label>
          <label>Test case id<input value={f.tc_id} onChange={(e) => setF({ ...f, tc_id: e.target.value })} placeholder="optional" /></label>
          <label>Test case title<input value={f.tc_title} onChange={(e) => setF({ ...f, tc_title: e.target.value })} /></label>
          <label className="check"><input type="checkbox" checked={f.reserve} onChange={(e) => setF({ ...f, reserve: e.target.checked })} />Reserve for me</label>
        </div>
        <p className="muted small">{tpl.available.find((t: J) => t.id === f.template_id)?.coverage}</p>
        <button className="primary" disabled={act.busy || !target || !app.can("tdm:request")} onClick={submit}>Request</button>
        {result && (
          <div className="tpl">
            <p>Request <code>{result.id}</code> <StatusBadge s={result.status === "FULFILLED" ? "pass" : result.status === "PARTIAL" || result.status === "PENDING_APPROVAL" ? "warn" : "fail"} /> {result.status}
              {result.approval && <span className="muted small"> · approved by {result.approval.by}</span>}</p>
            {[...result.reasons, ...result.notes].map((n: string) => <p key={n} className="muted small">• {n}</p>)}
            {result.datasets.map((x: J) => <p key={x.id}><Badge kind="info">{x.source}</Badge> <code>{x.id}</code> {Object.entries(x.handles).map(([k, v]) => `${k}: ${Array.isArray(v) ? (v as string[]).join(", ") : v}`).join(" · ")}</p>)}
          </div>)}
      </Card>

      {reqs.some((r) => r.status === "PENDING_APPROVAL") && (
        <Card title="Requests waiting for approval">
          <DataTable rows={reqs.filter((r) => r.status === "PENDING_APPROVAL")} cols={[
            { key: "id", title: "Request" }, { key: "requester", title: "By" }, { key: "tpl", title: "Scenario", render: (r) => r.spec.template_id },
            { key: "n", title: "Count", render: (r) => r.spec.count }, { key: "notes", title: "Why", render: (r) => r.notes.join("; ") },
            { key: "a", title: "", render: (r) => <div className="row"><button className="primary" disabled={!app.can("plan:approve") || r.requester === me} onClick={() => go(() => api.post(`/api/tdm/requests/${r.id}/approve`))}>Approve</button>
              <button disabled={!app.can("plan:approve")} onClick={() => go(() => api.post(`/api/tdm/requests/${r.id}/reject`, { reason: prompt("Reason") ?? "" }))}>Reject</button></div> }]} />
        </Card>)}

      <Card title="Catalog" actions={<div className="row">
        <select value={flt.template_id} onChange={(e) => setFlt({ ...flt, template_id: e.target.value })} aria-label="Scenario filter"><option value="">all scenarios</option>
          {tpl.available.map((t: J) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
        <select value={flt.state} onChange={(e) => setFlt({ ...flt, state: e.target.value })} aria-label="State filter"><option value="">active</option>
          {["AVAILABLE", "RESERVED", "CONSUMED", "EXPIRED", "RETIRED", "PURGED"].map((s) => <option key={s}>{s}</option>)}</select>
        <select value={flt.provenance} onChange={(e) => setFlt({ ...flt, provenance: e.target.value })} aria-label="Origin filter"><option value="">any origin</option>
          <option value="subset">subset</option><option value="synthetic">synthetic</option><option value="discovered">discovered</option></select></div>}>
        <DataTable rows={cat} pageSize={8} empty="No datasets yet: request test data or scan the target" cols={[
          { key: "name", title: "Dataset" }, { key: "state", title: "State", render: (r) => <><StatusBadge s={stateKind(r.state)} /> {r.state}{r.golden && <Badge kind="warn">golden</Badge>}</> },
          { key: "provenance", title: "Origin" },
          { key: "handles", title: "Handles", render: (r) => <span className="small mono">{Object.entries(r.handles).map(([k, v]) => `${k}: ${Array.isArray(v) ? (v as string[]).slice(0, 2).join(",") : v}`).join(" · ")}</span> },
          { key: "reserved_by", title: "Held by", render: (r) => (r.reserved_by ? `${r.reserved_by} until ${r.reserved_until?.slice(0, 10)}` : "—") },
          { key: "tc", title: "Test cases", render: (r) => r.test_cases.map((t: J) => t.id).join(", ") || "—" },
          { key: "act", title: "", render: rowActions }]} />
      </Card>

      <Card title="Scenarios not available yet">
        <ul className="checks">{tpl.planned.map((p: J) => <li key={p.id}><Badge kind="bad">planned</Badge> <strong>{p.name}</strong> <span className="muted">{p.reason}</span></li>)}</ul>
      </Card>
    </>
  );
}

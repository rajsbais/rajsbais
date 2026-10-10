import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Bars, Badge, Card, ErrorNote, Stat, Stepper, StatusBadge, TabList, TabPanel, useAction } from "../components";
import { useApp } from "../ctx";

const STEPS = ["Systems", "Scope", "Plan preview", "Conflicts", "Masking", "Approval", "Execute"];
const TYPES = ["SALES_ORDER", "DELIVERY", "BILLING", "FI_DOCUMENT", "PURCHASE_ORDER", "PRODUCTION_ORDER", "MATERIAL_DOCUMENT", "BOM", "ROUTING", "INSPECTION_LOT", "TRANSFER_ORDER", "STORAGE_BIN", "EWM_WAREHOUSE_ORDER", "EWM_BIN", "PROJECT", "MAINT_ORDER", "MAINT_NOTIFICATION", "EQUIPMENT", "FUNC_LOCATION", "EMPLOYEE", "FLIGHT", "CARRIER", "TRAVEL_CUSTOMER", "CUSTOMER", "VENDOR", "MATERIAL"];
const DOWN = ["DELIVERY", "BILLING", "FI_DOCUMENT", "MATERIAL_DOCUMENT", "INSPECTION_LOT", "MAINT_NOTIFICATION", "MAINT_ORDER", "TRANSFER_ORDER", "EWM_WAREHOUSE_ORDER"];

export default function Designer() {
  const app = useApp();
  const p = app.project;
  const [tab, setTab] = useState(0);
  const act = useAction();
  const [name, setName] = useState("CC1000 sales orders, last 90 days");
  const [src, setSrc] = useState(""); const [tgt, setTgt] = useState("");
  const [otype, setOtype] = useState("SALES_ORDER"); const [cc, setCc] = useState("1000"); const [pl, setPl] = useState(""); const [days, setDays] = useState(90);
  const [down, setDown] = useState<string[]>(["DELIVERY", "BILLING", "FI_DOCUMENT"]); const [tmpl, setTmpl] = useState("gdpr-standard");
  const [plan, setPlan] = useState<J>(null); const [conf, setConf] = useState<J>(null); const [mask, setMask] = useState<J>(null);
  const [dupPolicy, setDupPolicy] = useState("SKIP");

  useEffect(() => {
    setPlan(null); setConf(null); setMask(null);
    if (!p) { setTab(0); return; }
    if (p.has_plan) api.get(`/api/projects/${p.id}/plan`).then(setPlan).catch(() => undefined);
    if (p.has_conflicts) api.get(`/api/projects/${p.id}/conflicts`).then(setConf).catch(() => undefined);
    if (p.manifest) api.get(`/api/projects/${p.id}/masking`).then(setMask).catch(() => undefined);
  }, [p?.id, p?.status, p?.manifest_hash]); // eslint-disable-line react-hooks/exhaustive-deps

  const prod = app.systems.filter((s) => s.role === "PRD");
  const sources = app.systems; const targets = app.systems.filter((s) => s.writable_target);
  const refresh = async (fn: () => Promise<unknown>) => { await act.run(async () => { await fn(); await app.reload(); }); };
  const pid = p?.id;
  const step = !p ? 0 : !p.manifest ? 1 : !p.has_plan ? 2 : !p.has_conflicts ? 3 : p.status === "APPROVED" ? 6 : p.status === "PENDING_APPROVAL" ? 5 : 4;

  return (
    <>
      <Stepper steps={STEPS} active={step} />
      <TabList id="designer" tabs={STEPS} active={tab} onChange={setTab} label="Refresh design steps" />
      <ErrorNote error={act.error} />
      <TabPanel id="designer" active={tab}>

      {tab === 0 && (
        <Card title="1 · Select source and target">
          {p ? <p>Active project <strong>{p.name}</strong>: {p.source.sid}/{p.source.client} → {p.target.sid}/{p.target.client} <StatusBadge s={p.status} /></p> : null}
          <div className="form">
            <label>Project name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label>Source (read-only extraction)<select value={src} onChange={(e) => setSrc(e.target.value)}><option value="">choose…</option>
              {sources.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
            <label>Target (writable, non-production only)<select value={tgt} onChange={(e) => setTgt(e.target.value)}><option value="">choose…</option>
              {targets.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
          </div>
          <p className="muted small">Production systems ({prod.map((s) => s.label).join(", ") || "none"}) are never offered as targets; the API also refuses them.</p>
          <button className="primary" disabled={!src || !tgt || act.busy || !app.can("project:write")}
                  onClick={() => refresh(async () => { const r = await api.post("/api/projects", { name, source_id: src, target_id: tgt }); app.selectProject(r.id); setTab(1); })}>
            Create project
          </button>
        </Card>)}

      {tab === 1 && (
        <Card title="2 · Define scope (versioned manifest)">
          {!p ? <p className="muted">Create a project first.</p> : (
            <>
              <div className="form">
                <label>Root business object<select value={otype} onChange={(e) => setOtype(e.target.value)}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>
                <label>Company codes (comma separated)<input value={cc} onChange={(e) => setCc(e.target.value)} /></label>
                <label>Plants (comma separated, optional)<input value={pl} onChange={(e) => setPl(e.target.value)} /></label>
                <label>Created in the last N days (0 = no date filter)<input type="number" min={0} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label>
                <label>Masking template<select value={tmpl} onChange={(e) => setTmpl(e.target.value)}>
                  <option value="gdpr-standard">GDPR standard — pseudonymization</option><option value="gdpr-strict">GDPR strict — per-run anonymization</option></select></label>
              </div>
              <fieldset><legend>Include downstream documents</legend>
                {DOWN.map((d) => <label key={d} className="check"><input type="checkbox" checked={down.includes(d)} onChange={(e) => setDown(e.target.checked ? [...down, d] : down.filter((x) => x !== d))} />{d}</label>)}
              </fieldset>
              <button className="primary" disabled={act.busy || !app.can("plan:write")}
                onClick={() => refresh(async () => {
                  await api.put(`/api/projects/${pid}/manifest`, {
                    scope: { object_type: otype, company_codes: cc.split(",").map((x) => x.trim()).filter(Boolean), plants: pl.split(",").map((x) => x.trim()).filter(Boolean) },
                    last_days: days || null, include_downstream: down, masking_policy_id: tmpl,
                    conflict_policy: p.manifest?.conflict_policy ?? {}, instance_overrides: p.manifest?.instance_overrides ?? {} });
                  setTab(2); })}>
                Save manifest{p.manifest ? ` (v${p.manifest.version + 1})` : ""}
              </button>
              {p.manifest && <p className="muted small">Current manifest v{p.manifest.version} · hash <code>{p.manifest_hash.slice(0, 16)}…</code>. Any change invalidates the plan and approval.</p>}
            </>)}
        </Card>)}

      {tab === 2 && (
        <Card title="3 · Dependency expansion and volume preview" actions={<button className="primary" disabled={!p?.manifest || act.busy} onClick={() => refresh(async () => setPlan(await api.post(`/api/projects/${pid}/plan`)))}>Build plan</button>}>
          {!plan ? <p className="muted">Build the plan to expand dependencies from the selected scope.</p> : (
            <>
              <div className="grid stats">
                <Stat label="Business objects" value={plan.instances} /><Stat label="Rows" value={plan.total_rows} hint={`of ${plan.source_total_rows} in source`} />
                <Stat label="Volume" value={`${(plan.bytes / 1024).toFixed(1)} KB`} />
                <Stat label="Est. duration" value={`${plan.estimate.seconds}s`} hint={plan.estimate.basis === "calibrated" ? `calibrated, 80% ${plan.estimate.low}–${plan.estimate.high}s · simulator timing, not SAP` : "placeholder model, not a benchmark"} />
              </div>
              <div className="grid two">
                <div><h3>Objects by type</h3><Bars data={plan.by_type} /></div>
                <div><h3>Why they are in scope</h3><Bars data={plan.by_origin} />
                  <p className="muted small">ROOT = selected · REQUIRED = master/upstream dependency · DOWNSTREAM = document-flow successor</p></div>
              </div>
              <h3>Customizing the target must already contain</h3>
              <p>{Object.entries(plan.config_prerequisites).filter(([, v]) => (v as string[]).length > 0).map(([k, v]) => <Badge key={k}>{k}: {(v as string[]).join(", ")}</Badge>)}</p>
              <h3>Issues</h3>
              {plan.issues.length ? <ul className="checks">{plan.issues.map((i: J, n: number) => <li key={n}><StatusBadge s={i.severity} /> {i.message}</li>)}</ul> : <p className="muted">None</p>}
            </>)}
        </Card>)}

      {tab === 3 && (
        <Card title="4 · Target conflicts and policy" actions={<button className="primary" disabled={!p?.has_plan || act.busy} onClick={() => refresh(async () => setConf(await api.post(`/api/projects/${pid}/conflicts/analyze`)))}>Analyze conflicts</button>}>
          {!conf ? <p className="muted">Analyze the target to detect duplicates, number-range overlaps and configuration gaps.</p> : (
            <>
              <p>{conf.blocking ? <Badge kind="bad">blocking conflicts</Badge> : <Badge kind="ok">no blocking conflicts</Badge>} · {conf.executable_instances} objects will be written</p>
              <div className="grid two"><div><h3>Findings</h3><Bars data={conf.by_type} /></div><div><h3>Decisions</h3><Bars data={conf.decisions} /></div></div>
              <div className="form"><label>When an object exists in the target with different content
                <select value={dupPolicy} onChange={(e) => setDupPolicy(e.target.value)}>
                  <option value="FAIL">FAIL (block)</option><option value="SKIP">SKIP (keep target, quarantine dependent chain)</option>
                  <option value="QUARANTINE">QUARANTINE</option><option value="UPDATE">UPDATE (masters only)</option></select></label></div>
              <button disabled={act.busy || !app.can("plan:write")} onClick={() => refresh(async () => {
                await api.put(`/api/projects/${pid}/conflicts/policy`, { conflict_policy: { DUPLICATE_DIFFERENT: dupPolicy } });
                await api.post(`/api/projects/${pid}/plan`); setConf(await api.post(`/api/projects/${pid}/conflicts/analyze`)); })}>Apply policy and re-analyze</button>
              <p className="muted small">REPLACE is only possible per object with an approved exception. REMAP is not implemented.</p>
            </>)}
        </Card>)}

      {tab === 4 && (
        <Card title="5 · Sensitive-data masking">
          {!mask ? <p className="muted">Save a manifest and build a plan first.</p> : (
            <>
              <p>Policy: <strong>{mask.policy?.name ?? "none"}</strong> · {mask.uncovered === 0 ? <Badge kind="ok">all discovered fields covered</Badge> : <Badge kind="bad">{mask.uncovered} uncovered field(s)</Badge>}</p>
              <ul className="checks">{mask.discovered.map((d: J) => <li key={d.table + d.field}>{d.covered ? "✓" : "✗"} {d.table}.{d.field} <span className="muted">{d.category} · {d.source} · {d.confidence}</span></li>)}</ul>
              <button className="primary" disabled={!mask.uncovered || act.busy || !app.can("masking:write")} onClick={() => refresh(async () => {
                const adv = await api.get(`/api/projects/${pid}/agents/masking`);
                await api.post(`/api/projects/${pid}/masking/rules`, { rules: adv.add_rules });
                if (p?.has_plan) await api.post(`/api/projects/${pid}/conflicts/analyze`); })}>Apply advisor’s recommended rules</button>
              {!app.can("masking:write") && <p className="muted small">Your role cannot change masking policy.</p>}
              <p className="muted small">Release is blocked unless every discovered sensitive field has a rule.</p>
            </>)}
        </Card>)}

      {tab === 5 && (
        <Card title="6 · Approval (separation of duties)">
          {!p ? <p className="muted">No project.</p> : (
            <>
              <p>Status <StatusBadge s={p.status} />{p.approval && <> · approved by <strong>{p.approval.by}</strong> at {p.approval.at}</>}</p>
              <div className="row">
                <button disabled={act.busy || !p.has_conflicts || !app.can("plan:submit") || p.status !== "CONFLICTS_ANALYZED"} onClick={() => refresh(() => api.post(`/api/projects/${pid}/submit`))}>Submit for approval</button>
                <button className="primary" disabled={act.busy || p.status !== "PENDING_APPROVAL" || !app.can("plan:approve")} onClick={() => refresh(() => api.post(`/api/projects/${pid}/approve`, { comment: "approved in UI" }))}>Approve</button>
              </div>
              <p className="muted small">The creator/editor/submitter cannot approve. AI agents cannot approve. Switch user (sidebar) to Carol (Change approver) to approve.</p>
            </>)}
        </Card>)}

      {tab === 6 && (
        <Card title="7 · Execute (simulated)">
          {!p ? <p className="muted">No project.</p> : (
            <>
              <button className="primary" disabled={act.busy || p.status !== "APPROVED" || !app.can("run:execute")} onClick={() => refresh(async () => {
                const r = await api.post(`/api/projects/${pid}/execute`); app.setRunId(r.id); app.go("reconciliation"); })}>Execute selective refresh</button>
              {p.runs.length > 0 && <p>Runs: {p.runs.map((r: string) => <button key={r} onClick={() => { app.setRunId(r); app.go("execution"); }}>{r}</button>)}</p>}
              <p className="muted small">Execution writes only to the simulated non-production target, applies masking in-flight and stops at a release gate.</p>
            </>)}
        </Card>)}
      </TabPanel>
    </>
  );
}

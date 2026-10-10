import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, ErrorNote, Stat, Stepper, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

const STEPS = ["What to refresh", "Prepare", "Submit", "Approval", "Run", "Result"];
const EARLY = ["DRAFT", "PLANNED", "CONFLICTS_ANALYZED", "PENDING_APPROVAL", "APPROVED"];

interface Scenario { id: string; label: string; hint: string; type: string; company: string; plants: string; days: number; down: string[]; template?: string; needs?: string }
const SCENARIOS: Scenario[] = [
  { id: "sales", label: "Sales orders", hint: "With deliveries, billing documents and accounting documents", type: "SALES_ORDER", company: "1000", plants: "", days: 90, down: ["DELIVERY", "BILLING", "FI_DOCUMENT"] },
  { id: "purchasing", label: "Purchase orders", hint: "With their vendors and materials", type: "PURCHASE_ORDER", company: "1000", plants: "", days: 90, down: [] },
  { id: "production", label: "Production orders", hint: "With BOMs, routings, components and goods movements", type: "PRODUCTION_ORDER", company: "1000", plants: "", days: 90, down: ["MATERIAL_DOCUMENT"] },
  { id: "maintenance", label: "Maintenance orders", hint: "With notifications, equipment and locations", type: "MAINT_ORDER", company: "", plants: "1000", days: 0, down: [] },
  { id: "quality", label: "Inspection lots", hint: "With results, usage decisions and their orders", type: "INSPECTION_LOT", company: "", plants: "1000", days: 0, down: [] },
  { id: "projects", label: "Projects", hint: "With the WBS hierarchy and costs", type: "PROJECT", company: "1000", plants: "", days: 0, down: [] },
  { id: "employees", label: "Employees (HR)", hint: "Special-category personal data: anonymised on every run", type: "EMPLOYEE", company: "1000", plants: "", days: 0, down: [], template: "gdpr-strict", needs: "hr:copy" },
];

export default function Guided() {
  const app = useApp();
  const p = app.project;
  const act = useAction();
  const [sc, setSc] = useState(SCENARIOS[0].id);
  const scenario = SCENARIOS.find((s) => s.id === sc)!;
  const [src, setSrc] = useState(""); const [tgt, setTgt] = useState("");
  const [company, setCompany] = useState(scenario.company); const [plants, setPlants] = useState(scenario.plants); const [days, setDays] = useState(scenario.days);
  const [prep, setPrep] = useState<J>(null);
  const [rec, setRec] = useState<J>(null);
  const [run, setRun] = useState<J>(null);

  const sources = app.systems; const targets = app.systems.filter((s) => s.writable_target);
  useEffect(() => { if (!src && sources[0]) setSrc((sources.find((s) => s.role === "PRD") ?? sources[0]).id); }, [sources.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!tgt && targets[0]) setTgt(targets[0].id); }, [targets.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const pick = (id: string) => { const s = SCENARIOS.find((x) => x.id === id)!; setSc(id); setCompany(s.company); setPlants(s.plants); setDays(s.days); };

  const step = !p ? 0 : !EARLY.includes(p.status) ? 5 : p.status === "APPROVED" ? 4 : p.status === "PENDING_APPROVAL" ? 3 : p.status === "CONFLICTS_ANALYZED" ? 2 : 1;
  useEffect(() => { setPrep(null); setRec(null); setRun(null); }, [p?.id]);
  useEffect(() => {
    if ((step === 1 || step === 2) && p?.has_plan) {  // nothing to read before a plan exists
      void (async () => {
        try { setPrep({ plan: await api.get(`/api/projects/${p!.id}/plan`), conf: p!.has_conflicts ? await api.get(`/api/projects/${p!.id}/conflicts`) : null, mask: await api.get(`/api/projects/${p!.id}/masking`) }); } catch { /* shown by the step itself */ }
      })();
    }
    if (step === 5 && app.runId) {
      void (async () => { try { setRun(await api.get(`/api/runs/${app.runId}`)); setRec(await api.get(`/api/runs/${app.runId}/reconciliation`)); } catch { /* none yet */ } })();
    }
  }, [step, p?.id, p?.status, app.runId]); // eslint-disable-line react-hooks/exhaustive-deps

  const go = (fn: () => Promise<unknown>) => act.run(async () => { await fn(); await app.reload(); });
  const create = () => go(async () => { const r = await api.post("/api/projects", { name: `${scenario.label} · guided`, source_id: src, target_id: tgt }); app.selectProject(r.id); });
  const prepare = () => go(async () => {
    const id = p!.id;
    await api.put(`/api/projects/${id}/manifest`, {
      scope: { object_type: scenario.type, company_codes: company.split(",").map((x) => x.trim()).filter(Boolean), plants: plants.split(",").map((x) => x.trim()).filter(Boolean) },
      last_days: days || null, include_downstream: scenario.down, masking_policy_id: scenario.template ?? "gdpr-standard", conflict_policy: {}, instance_overrides: {} });
    await api.post(`/api/projects/${id}/plan`);
    let mask = await api.get(`/api/projects/${id}/masking`);
    if (mask.uncovered > 0 && app.can("masking:write")) {
      const adv = await api.get(`/api/projects/${id}/agents/masking`);
      await api.post(`/api/projects/${id}/masking/rules`, { rules: adv.add_rules });
      mask = await api.get(`/api/projects/${id}/masking`);
    }
    let conf = await api.post(`/api/projects/${id}/conflicts/analyze`);
    if (conf.blocking) {  // the careful default: an object that already exists in the target with different content is kept, never overwritten
      await api.put(`/api/projects/${id}/conflicts/policy`, { conflict_policy: { DUPLICATE_DIFFERENT: "SKIP" } });
      await api.post(`/api/projects/${id}/plan`);
      conf = await api.post(`/api/projects/${id}/conflicts/analyze`);
    }
    setPrep({ plan: await api.get(`/api/projects/${id}/plan`), conf: await api.get(`/api/projects/${id}/conflicts`), mask });
  });
  const blockers = prep ? (prep.plan?.issues ?? []).filter((i: J) => i.severity === "blocking").length + (prep.conf?.blocking ? 1 : 0) : 0;
  const unmasked = prep?.mask?.uncovered ?? 0;
  const checks: J[] = rec?.checks ?? [];
  const failed = checks.filter((c) => c.status === "fail");

  return (
    <>
      <Stepper steps={STEPS} active={step} />
      <ErrorNote error={act.error} />

      {step === 0 && (
        <Card title="1 · What do you want to refresh?">
          <p className="muted">Pick the business area. The platform then works out everything that belongs with it, and nothing is written until a second person has approved.</p>
          <fieldset><legend>Business area</legend>
            <div className="sol-tiles">
              {SCENARIOS.map((s) => (
                <label key={s.id} className="check"><input type="radio" name="scenario" checked={sc === s.id} onChange={() => pick(s.id)} />
                  <span><strong>{s.label}</strong> <span className="muted small">{s.hint}</span></span></label>))}
            </div>
          </fieldset>
          <div className="form">
            <label>From (read-only)<select value={src} onChange={(e) => setSrc(e.target.value)}>{sources.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
            <label>Into (non-production only)<select value={tgt} onChange={(e) => setTgt(e.target.value)}>{targets.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
            <label>Company codes (comma separated)<input value={company} onChange={(e) => setCompany(e.target.value)} /></label>
            <label>Plants (comma separated)<input value={plants} onChange={(e) => setPlants(e.target.value)} /></label>
            <label>Created in the last N days (0 = all)<input type="number" min={0} value={days} onChange={(e) => setDays(Number(e.target.value))} /></label>
          </div>
          {scenario.needs && !app.can(scenario.needs) && <p className="banner">This area needs the permission <code>{scenario.needs}</code>, which your role does not have.</p>}
          <button className="primary" disabled={!src || !tgt || act.busy || !app.can("project:write")} onClick={create}>Continue</button>
        </Card>)}

      {step === 1 && p && (
        <Card title="2 · Prepare the refresh">
          <p>Project <strong>{p.name}</strong>: {p.source.sid}/{p.source.client} → {p.target.sid}/{p.target.client} <StatusBadge s={p.status} /></p>
          <p className="muted">One click does the groundwork: it saves the scope, expands everything that belongs with the objects, checks the target for conflicts, and adds the recommended masking rules for personal data. Where an object already exists in the target with different content, it is kept and never overwritten.</p>
          {!prep?.conf && <button className="primary" disabled={act.busy || !app.can("plan:write")} onClick={prepare}>Prepare</button>}
          {prep?.conf && (<>
            <div className="grid stats">
              <Stat label="Business objects" value={prep.plan.instances} /><Stat label="Rows" value={prep.plan.total_rows} />
              <Stat label="Blocking issues" value={blockers} /><Stat label="Unmasked sensitive fields" value={unmasked} />
            </div>
            {blockers ? <p className="banner">Blocking issues must be fixed before approval. Change the scope, or open the <button onClick={() => app.go("conflicts")}>Conflicts</button> screen to decide what to do.</p> : null}
          </>)}
        </Card>)}

      {step === 2 && p && (
        <Card title="3 · Review and submit">
          {prep && (<div className="grid stats">
            <Stat label="Business objects" value={prep.plan.instances} /><Stat label="Rows" value={prep.plan.total_rows} />
            <Stat label="Will be written" value={prep.conf?.executable_instances ?? "—"} hint="objects" /><Stat label="Unmasked sensitive fields" value={unmasked} />
          </div>)}
          {blockers > 0 || unmasked > 0
            ? <p className="banner">Not ready: {blockers} blocking issue(s), {unmasked} sensitive field(s) without a masking rule. {unmasked > 0 && !app.can("masking:write") ? "Your role cannot add masking rules; ask a data steward." : ""}</p>
            : <p className="muted">Nothing blocks this refresh. Submitting asks a second person to approve it; the scope can no longer change without a new approval.</p>}
          <div className="row">
            <button className="primary" disabled={act.busy || !app.can("plan:submit")} onClick={() => go(() => api.post(`/api/projects/${p.id}/submit`))}>Submit for approval</button>
            <button onClick={() => app.go("designer")}>Open in the Selective designer</button>
          </div>
        </Card>)}

      {step === 3 && p && (
        <Card title="4 · Approval">
          <p>Waiting for approval. <StatusBadge s={p.status} /></p>
          <p className="muted">The person who prepared or submitted this cannot approve it, and AI agents cannot approve. Sign in as a change approver (in the demo: switch user to <strong>Carol</strong>) to approve.</p>
          <button className="primary" disabled={act.busy || !app.can("plan:approve")} onClick={() => go(() => api.post(`/api/projects/${p.id}/approve`, { comment: "approved in the guided refresh" }))}>Approve</button>
          {!app.can("plan:approve") && <p className="muted small">Your role cannot approve.</p>}
        </Card>)}

      {step === 4 && p && (
        <Card title="5 · Run the refresh">
          <p>Approved{p.approval ? <> by <strong>{p.approval.by}</strong></> : null}. Running writes only to the non-production target, masks personal data on the way, and stops at a release check.</p>
          <button className="primary" disabled={act.busy || !app.can("run:execute")} onClick={() => go(async () => { const r = await api.post(`/api/projects/${p.id}/execute`); app.setRunId(r.id); })}>Run the refresh</button>
          {!app.can("run:execute") && <p className="muted small">Your role cannot run refreshes. Switch back to the person who prepared it.</p>}
        </Card>)}

      {step === 5 && p && (
        <Card title="6 · Result">
          {!run ? <p className="muted">No run selected.</p> : (<>
            <div className="grid stats">
              <Stat label="Run" value={<code>{run.id}</code>} /><Stat label="Status" value={<StatusBadge s={run.status} />} />
              <Stat label="Release" value={<Badge kind={run.release === "RELEASED" ? "ok" : "warn"}>{run.release}</Badge>} />
              <Stat label="Checks" value={`${checks.length - failed.length} / ${checks.length} passed`} />
            </div>
            {failed.length > 0 && <ul className="checks">{failed.map((c) => <li key={c.id}><StatusBadge s="fail" /> {c.id}: {c.name}{c.detail ? ` (${c.detail})` : ""}</li>)}</ul>}
            <div className="row">
              <button onClick={() => app.go("reconciliation")}>Open the reconciliation</button>
              <button onClick={() => app.go("execution")}>Open the run</button>
              <button className="primary" onClick={() => { app.selectProject(null); app.setRunId(null); }}>Start another refresh</button>
            </div>
          </>)}
        </Card>)}
    </>
  );
}

import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Bars } from "../components";
import { useApp } from "../ctx";
import Dashboard from "./Dashboard";

export default function ControlTower() {
  const app = useApp();
  const p = app.project;
  const [plan, setPlan] = useState<J>(null);
  const [conf, setConf] = useState<J>(null);
  const [strat, setStrat] = useState<J>(null);
  useEffect(() => {
    setPlan(null); setConf(null); setStrat(null);
    if (!p) return;
    if (p.has_plan) {
      api.get(`/api/projects/${p.id}/plan`).then(setPlan).catch(() => undefined);
      api.post(`/api/projects/${p.id}/agents/strategy`, { source_gb: 500, freshness_days: 7 }).then(setStrat).catch(() => undefined);
    }
    if (p.has_conflicts) api.get(`/api/projects/${p.id}/conflicts`).then(setConf).catch(() => undefined);
  }, [p?.id, p?.status, p?.manifest_hash]); // eslint-disable-line react-hooks/exhaustive-deps

  const scope = p?.manifest?.scope;
  const blockers = (plan?.issues ?? []).filter((i: J) => i.severity === "blocking").length + (conf?.findings ?? []).filter((f: J) => f.severity === "blocking").length;
  let pill = "Choose a project"; let pillKind = "";
  let text = "Create a refresh project, pick a source and a writable non-production target, then define the scope.";
  if (p && !p.manifest) { pill = "Define scope"; text = "The project has no manifest yet. Select a company code and date window in the Selective designer."; }
  else if (p && !p.has_plan) { pill = "Build plan"; text = "Expand the selected scope into a dependency-complete plan before choosing a method."; }
  else if (plan && plan.instances === 0) { pill = "Do not run"; pillKind = "bad"; text = "The scope has no objects. Widen the company code or the date window before choosing a method."; }
  else if (plan && blockers > 0) { pill = "Resolve blockers"; pillKind = "warn"; text = `${blockers} blocking issue(s) must be resolved by policy, exception or scope change before approval.`; }
  else if (strat) { pill = strat.recommendation.replace(/_/g, " "); pillKind = "ok"; text = strat.rationale.join(". ") + "."; }

  return (
    <>
      <div className="eyebrow">Control tower</div>
      <h2 className="hero">Refresh only what the test needs</h2>
      <p className="lede">
        Keystone plans a business-complete slice from {p ? `${p.source.role} client ${p.source.client}` : "production"} into {p ? `${p.target.sid} client ${p.target.client}` : "a non-production target"}.
        The landscape, documents and load are synthetic {p ? (p.source.family === "S4" ? "S/4HANA" : "ECC") : "ECC and S/4HANA"} data. Nothing here talks to a live SAP system.
      </p>
      <div className="grid two tiles">
        <div className="tile"><div className="eyebrow">Source</div><div className="big mono">{p ? `${p.source.role} / ${p.source.client}` : "—"}</div><div className="muted">{p ? `${p.source.product} · ${p.source.sid}` : "No project selected"}</div></div>
        <div className="tile"><div className="eyebrow">Target</div><div className="big mono">{p ? `${p.target.sid} / ${p.target.client}` : "—"}</div><div className="muted">{p ? (p.target.writable_target_allowed ? "Writable non-prod" : "Locked") : ""}</div></div>
        <div className="tile"><div className="eyebrow">Plan</div><div className="big mono">{plan ? `${plan.instances} objects` : "Not built"}</div>
          <div className="muted">{scope ? `Company ${scope.company_codes?.join(", ") || "all"}${scope.date_from ? ` · ${Math.round((Date.parse(scope.date_to) - Date.parse(scope.date_from)) / 86400000)} days` : ""}` : "No scope"}</div></div>
        <div className="tile"><div className="eyebrow">Blockers</div><div className="big mono">{plan ? blockers : "—"}</div><div className="muted">{p?.status === "APPROVED" ? "Approved" : "Not approved"}</div></div>
      </div>
      <section className="card">
        <header><h3>Recommended path</h3><span className={`pill ${pillKind}`}>{pill}</span></header>
        <p className="body">{text}</p>
        <div className="row">
          <button className="primary" onClick={() => app.go("designer")}>{p?.has_plan ? "Open selective designer" : "Build company 1000 plan"}</button>
          <button onClick={() => app.go("catalog")}>Open test catalog</button>
        </div>
      </section>
      <section className="card">
        <header><h3>Slice volume</h3></header>
        {plan ? <div className="grid two"><Bars data={plan.by_type} /><div><p className="mono big">{plan.total_rows} rows · {(plan.bytes / 1024).toFixed(1)} KB</p>
          <p className="muted">Estimated {plan.estimate.seconds}s {plan.estimate.basis === "calibrated" ? <Badge kind="pass">calibrated</Badge> : <Badge kind="warn">placeholder model</Badge>}</p></div></div>
          : <p className="body">Build a plan to see object counts. Estimates are planning figures, not a throughput benchmark.</p>}
      </section>
      <h3 className="section-title">Portfolio</h3>
      <Dashboard />
    </>
  );
}

import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Select, Stat, Table } from "../components/ui";

export default function Cutover() {
  const { projectId } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const [mid, setMid] = useState("");
  const id = mid || manifests.data?.[0]?.id;
  const rb = useApi<any>(id ? `/manifests/${id}/cutover/runbook` : null, undefined, [id]);
  const [risk, setRisk] = useState<any>(null); const [err, setErr] = useState<string | null>(null);
  const assess = async () => { setErr(null); try { setRisk(await api(`/projects/${projectId}/agents/cutover_risk/run`, { body: { context: { manifest_id: id } } })); } catch (e: any) { setErr(e.message); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  if (!manifests.data?.length) return <Banner>Create a manifest first.</Banner>;
  const r = rb.data;
  return (
    <div>
      <Banner kind="warn">Cutover Command Center is <b>PARTIAL</b>: runbook generation, dependency-aware scheduling, critical path, downtime forecast, rollback gates and go/no-go criteria are implemented; live execution tracking, incident escalation and resource assignment are planned. Forecasts are template-based, not measured.</Banner>
      <div className="row"><Select value={id} onChange={setMid} options={(manifests.data || []).map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} /><button className="secondary" onClick={assess}>Assess cutover risk (agent)</button></div>
      <ErrorBox error={err || rb.error} />
      {r && <>
        <div className="stats"><Stat label="Total plan" value={`${(r.total_minutes / 60).toFixed(1)} h`} /><Stat label="Forecast downtime" value={`${(r.forecast_downtime_minutes / 60).toFixed(1)} h`} sub="critical-path downtime tasks" /><Stat label="Point of no return" value={r.point_of_no_return} /><Stat label="Critical path" value={r.critical_path.join(" → ")} />{risk && <Stat label="Risk" value={<Pill value={risk.proposal.band} />} sub={`score ${risk.proposal.score}`} />}</div>
        <Card title="Runbook"><Table cols={[{ k: "id", h: "#" }, { k: "name", h: "Task" }, { k: "phase", h: "Phase" }, { k: "depends_on", h: "Depends", r: (x) => x.depends_on.join(", ") }, { k: "est_minutes", h: "Est. min" }, { k: "earliest_start_min", h: "Start" }, { k: "earliest_finish_min", h: "Finish" }, { k: "downtime", h: "Downtime", r: (x) => (x.downtime ? "yes" : "") }, { k: "irreversible", h: "Irreversible", r: (x) => (x.irreversible ? <Pill value="IRREVERSIBLE" /> : "") }, { k: "owner", h: "Owner" }, { k: "sign_off", h: "Sign-off" }, { k: "cp", h: "Critical", r: (x) => (r.critical_path.includes(x.id) ? "●" : "") }]} rows={r.tasks} /><p className="muted">{r.basis}</p></Card>
        <div className="grid2"><Card title="Rollback decision gates"><KV obj={r.rollback} /></Card><Card title="Go / no-go criteria">{risk ? <Table cols={[{ k: "criterion", h: "Criterion" }, { k: "met", h: "Met", r: (x) => <Pill value={x.met ? "PASS" : "FAIL"} /> }, { k: "note", h: "Note" }]} rows={risk.proposal.go_no_go_criteria} /> : <div className="muted">Run the risk assessment.</div>}{risk && <KV obj={risk.proposal.factors} />}</Card></div>
      </>}
    </div>
  );
}

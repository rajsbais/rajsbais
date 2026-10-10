import { Link } from "react-router-dom";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, Hero, Pill } from "../components/ui";
import { APPROACHES, journeyPhases, type Deliverable } from "../lib";

/** Transformation journey: the platform's own five phases with every deliverable's status read from the project's
 *  state, and the three S/4HANA approaches with what the platform does in each. Original structure; no vendor method. */
export function useJourney() {
  const { projectId, project, source } = useProjectDetails();
  const disc = useApi<any>(source ? `/systems/${source.id}/discovery` : null, undefined, [source?.id]);
  const graph = useApi<any>(source ? `/systems/${source.id}/graph/stats` : null, undefined, [source?.id]);
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const latestM = (manifests.data || [])[0];
  const completeness = useApi<any>(latestM ? `/manifests/${latestM.id}/carveout/completeness` : null, undefined, [latestM?.id]);
  const rehearsals = useApi<any[]>(latestM ? `/manifests/${latestM.id}/cutover/rehearsals` : null, undefined, [latestM?.id]);
  const baseline = (runs.data || []).find((r) => r.status === "COMPLETED" && r.metrics?.kind !== "DELTA");
  const delta = useApi<any>(baseline ? `/runs/${baseline.id}/delta` : null, undefined, [baseline?.id]);
  const evidence = useApi<any>(baseline ? `/runs/${baseline.id}/evidence` : null, undefined, [baseline?.id]);
  const approvals = useApi<any[]>(baseline ? "/audit/approvals" : null, { subject_id: baseline?.id }, [baseline?.id]);
  const verify = useApi<any>("/audit/verify");
  const phases = journeyPhases({
    discovery: !!disc.data?.summary,
    graph: (graph.data?.nodes || 0) > 0,
    manifests: manifests.data || [],
    rulesets: rulesets.data || [],
    runs: runs.data || [],
    completeness: completeness.data,
    rehearsals: rehearsals.data || [],
    delta: delta.data,
    evidence: evidence.data,
    approvals: approvals.data || [],
    auditChain: verify.data ? !!verify.data.ok : null,
  });
  return { project, phases, scenario: project?.scenario_type };
}

function DeliverableRow({ d }: { d: Deliverable }) {
  return <li className={`deliv ${d.status.toLowerCase()}`}><span className="dot" /><span>{d.to ? <Link to={d.to}>{d.label}</Link> : d.label}</span><span className="muted">{d.detail}</span></li>;
}

export function JourneyStrip() {
  const { phases } = useJourney();
  return <div className="jstrip">{phases.map((p, i) => <Link key={p.id} to="/journey" className={`jchip ${p.status.toLowerCase()}`}><span className="n">{i + 1}</span><span>{p.title}</span><Pill value={p.status} /></Link>)}</div>;
}

export default function Journey() {
  const { project, phases, scenario } = useJourney();
  if (!project) return <Banner>Select or create a project first.</Banner>;
  const done = phases.filter((p) => p.status === "DONE").length;
  return (
    <div>
      <Hero title="Transformation journey" subtitle={`${done} of ${phases.length} phases complete for ${project.name} · every deliverable's status is read from the platform's own state, nothing is asserted by hand`} />
      <div className="phases">
        {phases.map((p, i) => <section key={p.id} className={`phase ${p.status.toLowerCase()}`}>
          <header><span className="n">{i + 1}</span><h3>{p.title}</h3><Pill value={p.status} /></header>
          <p className="muted">{p.lead}</p>
          <ul>{p.deliverables.map((d) => <DeliverableRow key={d.label} d={d} />)}</ul>
        </section>)}
      </div>
      <div className="outputs"><span className="muted">Validated outputs</span>{["Landscape snapshot", "Hashed manifest", "Approved rule set", "Reconciled run", "Rehearsal verdict", "Evidence package", "Hash-chained audit trail"].map((o) => <span key={o} className="pill neutral">{o}</span>)}</div>
      <Card title="S/4HANA approaches and what the platform does in each">
        <div className="approaches">{APPROACHES.map((a) => <section key={a.id} className={`approach ${a.scenarios.includes(scenario || "") ? "on" : ""}`}><h4>{a.title}{a.scenarios.includes(scenario || "") && <Pill value="THIS PROJECT" />}</h4><p className="muted">{a.summary}</p><ul>{a.platform.map((x) => <li key={x}>{x}</li>)}</ul></section>)}</div>
        <p className="muted">Project scenario: <b>{scenario || "-"}</b>. The approach names are the SAP ones (system conversion, new implementation, selective data transition); the platform covers the data side of all three and never converts or installs a system itself.</p>
      </Card>
    </div>
  );
}

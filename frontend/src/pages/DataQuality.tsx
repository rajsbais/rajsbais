import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Pill, Stat, Table } from "../components/ui";

export default function DataQuality() {
  const { projectId } = useProjectDetails();
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => { setErr(null); try { setRes(await api(`/projects/${projectId}/agents/data_quality/run`, { body: { context: {} } })); } catch (e: any) { setErr(e.message); } };
  const last = runs.data?.[0];
  const exc = useApi<any[]>(last ? `/runs/${last.id}/exceptions` : null, undefined, [last?.id]);
  if (!projectId) return <Banner>Select a project.</Banner>;
  return (
    <div>
      <Card title="Source data quality (Data Quality Agent)" actions={<button onClick={run}>Analyse source</button>}>
        <ErrorBox error={err} />
        {res && <><div className="stats"><Stat label="Quality score" value={res.proposal.score} /><Stat label="Issue types" value={res.proposal.issues.length} /><Stat label="Confidence" value={res.confidence} /></div><Table cols={[{ k: "check", h: "Check" }, { k: "count", h: "Count" }, { k: "samples", h: "Samples", r: (r) => (r.samples || []).join(", ") }]} rows={res.proposal.issues} empty="No issues detected" /></>}
      </Card>
      <Card title={last ? `Transformation / load exceptions of run ${last.id.slice(0, 8)}` : "Exceptions"}><Table cols={[{ k: "stage", h: "Stage" }, { k: "table", h: "Table" }, { k: "key", h: "Key" }, { k: "rule", h: "Rule" }, { k: "severity", h: "Severity", r: (r) => <Pill value={r.severity} /> }, { k: "message", h: "Message" }, { k: "disposition", h: "Disposition" }]} rows={exc.data || []} empty="No exceptions" /></Card>
    </div>
  );
}

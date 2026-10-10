import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Bars, Card, DataTable, Stat, StatusBadge } from "../components";
import { useApp } from "../ctx";

export default function Conflicts() {
  const { project } = useApp();
  const [c, setC] = useState<J>(null);
  const [sug, setSug] = useState<J>(null);
  useEffect(() => {
    setC(null); setSug(null);
    if (project?.has_conflicts) {
      api.get(`/api/projects/${project.id}/conflicts`).then(setC).catch(() => undefined);
      api.get(`/api/projects/${project.id}/agents/conflicts`).then(setSug).catch(() => undefined);
    }
  }, [project?.id, project?.status, project?.manifest_hash]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!project?.has_conflicts || !c) return <Card><p className="muted">Run “Analyze conflicts” in the Selective Refresh Designer.</p></Card>;
  return (
    <>
      <div className="grid stats">
        <Stat label="Findings" value={c.findings.length} /><Stat label="Blocking" value={c.findings.filter((f: J) => f.severity === "blocking").length} />
        <Stat label="Objects written" value={c.executable_instances} /><Stat label="Number ranges to raise" value={Object.keys(c.number_range_adjustments).length} />
      </div>
      <div className="grid two">
        <Card title="Findings by type"><Bars data={c.by_type} /></Card>
        <Card title="Decisions per business object"><Bars data={c.decisions} /></Card>
      </div>
      <Card title="Findings">
        <DataTable rows={c.findings} cols={[
          { key: "severity", title: "Severity", render: (f) => <StatusBadge s={f.severity} /> }, { key: "type", title: "Type" },
          { key: "instance", title: "Object", render: (f) => f.instance ?? "—" }, { key: "action", title: "Action", render: (f) => (f.action ? <StatusBadge s={f.action} /> : "—") },
          { key: "message", title: "Explanation" },
          { key: "owner", title: "Owner", render: (f) => f.details?.owner ?? "" }]} pageSize={10} />
      </Card>
      {sug && (
        <Card title="Policy advisor (rule-based, human approval required)">
          <p>Recommended: <strong>{sug.recommended_policy}</strong></p>
          <DataTable rows={sug.alternatives} cols={[
            { key: "policy", title: "Policy" }, { key: "blocking", title: "Blocked?", render: (a) => <StatusBadge s={a.blocking ? "fail" : "pass"} /> },
            { key: "executable_objects", title: "Objects loadable" }, { key: "quarantined", title: "Quarantined" }]} />
          <p className="muted small">{sug.note}</p>
        </Card>)}
    </>
  );
}

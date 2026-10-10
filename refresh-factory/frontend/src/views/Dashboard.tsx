import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Bars, Card, DataTable, ErrorNote, Stat, StatusBadge, useAction } from "../components";
import { useApp } from "../ctx";

export default function Dashboard() {
  const app = useApp();
  const [caps, setCaps] = useState<J[]>([]);
  const act = useAction();
  useEffect(() => { api.get("/api/capabilities").then(setCaps).catch(() => setCaps([])); }, []);
  const byStatus: Record<string, number> = {};
  caps.forEach((c) => { byStatus[c.status] = (byStatus[c.status] ?? 0) + 1; });
  const byProject: Record<string, number> = {};
  app.projects.forEach((p) => { byProject[p.status] = (byProject[p.status] ?? 0) + 1; });
  return (
    <>
      <div className="grid stats">
        <Stat label="Registered systems" value={app.systems.length} />
        <Stat label="Refresh projects" value={app.projects.length} />
        <Stat label="Modules implemented (simulated)" value={`${byStatus["implemented-simulated"] ?? 0} / ${caps.length}`} hint="none are production-ready against real SAP" />
        <Stat label="Catalogued / planned" value={`${(byStatus["catalogued"] ?? 0)} / ${(byStatus["planned"] ?? 0)}`} />
      </div>
      {!app.systems.length && (
        <Card title="Get started">
          <p>No systems are registered. Load the synthetic ECC 6.0 EHP8 landscape (production source EP1/100, QA target EQ1/200).</p>
          <button className="primary" disabled={act.busy || !app.can("system:write")} onClick={() => act.run(async () => { await api.post("/api/demo/bootstrap"); await app.reload(); })}>
            Load synthetic landscape
          </button>
          {!app.can("system:write") && <p className="muted">Your role cannot register systems (needs system:write).</p>}
          <ErrorNote error={act.error} />
        </Card>)}
      <div className="grid two">
        <Card title="Projects by status">{Object.keys(byProject).length ? <Bars data={byProject} /> : <p className="muted">No projects yet.</p>}</Card>
        <Card title="Capability maturity">{caps.length ? <Bars data={byStatus} /> : <p className="muted">Loading…</p>}</Card>
      </div>
      <Card title="Refresh projects">
        <DataTable rows={app.projects} cols={[
          { key: "name", title: "Project" },
          { key: "status", title: "Status", render: (p) => <StatusBadge s={p.status} /> },
          { key: "route", title: "Route", render: (p) => `${p.source.sid}/${p.source.client} → ${p.target.sid}/${p.target.client}` },
          { key: "created_by", title: "Created by" },
          { key: "open", title: "", render: (p) => <button onClick={() => { app.selectProject(p.id); app.go("designer"); }}>Open</button> },
        ]} empty="No refresh projects yet" />
      </Card>
    </>
  );
}

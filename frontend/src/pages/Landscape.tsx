import { useState } from "react";
import { api, fmtBytes, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Stat, Table, Tabs } from "../components/ui";

export default function Landscape() {
  const { project, source, target, reload } = useProjectDetails();
  const [sys, setSys] = useState<string | null>(null);
  const sysId = sys || source?.id;
  const system = project?.systems?.find((s: any) => s.id === sysId);
  const disc = useApi<any>(sysId ? `/systems/${sysId}/discovery` : null, undefined, [sysId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => { setBusy(true); setErr(null); try { await api(`/systems/${sysId}/discover`, { method: "POST" }); disc.reload(); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const s = disc.data?.summary;
  if (!project) return <Banner>Select or create a project first.</Banner>;
  return (
    <div>
      <Card title="Systems" actions={<><select value={sysId || ""} onChange={(e) => setSys(e.target.value)}>{project.systems.map((x: any) => <option key={x.id} value={x.id}>{x.role} {x.sid}/{x.client} ({x.product} {x.release})</option>)}</select> <button disabled={busy || !sysId} onClick={run}>{busy ? "Discovering…" : "Run discovery"}</button></>}>
        <Table cols={[{ k: "role", h: "Role" }, { k: "sid", h: "SID" }, { k: "client", h: "Client" }, { k: "product", h: "Product" }, { k: "release", h: "Release" }, { k: "database", h: "Database" }, { k: "connector", h: "Connector" }, { k: "connector_status", h: "Connector status", r: (r) => <Pill value={r.connector_status} /> }, { k: "logical_system", h: "Logical system" }]} rows={[source, target].filter(Boolean)} />
        <ErrorBox error={err} />
      </Card>
      {disc.error && <Banner>No discovery snapshot for {system?.sid}. Run discovery to collect release, organisational, table and object information.</Banner>}
      {s && <>
        <div className="stats"><Stat label="Tables" value={s.tables.count} sub={`${s.tables.custom} custom (Z/Y)`} /><Stat label="Rows" value={fmtNum(s.tables.total_rows)} sub={fmtBytes(s.tables.est_bytes)} /><Stat label="Company codes" value={s.org_units.COMPANY_CODE} sub={`${s.org_units.PLANT} plants, ${s.org_units.CONTROLLING_AREA} controlling areas`} /><Stat label="Interfaces" value={s.interfaces.length} sub={`${s.jobs.length} background jobs`} /><Stat label="S/4 impact items" value={s.s4_impacts.length} /><Stat label="Complexity" value={<Pill value={s.complexity.band} />} sub={`score ${s.complexity.score}`} /></div>
        <Tabs tabs={[
          { id: "sys", label: "System facts", content: <Card><KV obj={s.system} /></Card> },
          { id: "tables", label: "Largest tables", content: <Card><Table cols={[{ k: "table", h: "Table" }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "est_bytes", h: "Est. size", r: (r) => fmtBytes(r.est_bytes) }, { k: "custom", h: "Custom", r: (r) => (r.custom ? "Z/Y" : "") }, { k: "s4_status", h: "S/4HANA status", r: (r) => <Pill value={r.s4_status} /> }]} rows={s.tables.top} /></Card> },
          { id: "if", label: "Interfaces & jobs", content: <div className="grid2"><Card title="Interfaces"><Table cols={[{ k: "type", h: "Type" }, { k: "name", h: "Name" }, { k: "target", h: "Target" }, { k: "purpose", h: "Purpose" }]} rows={s.interfaces} /></Card><Card title="Background jobs"><Table cols={[{ k: "name", h: "Job" }, { k: "user", h: "User" }, { k: "periodic", h: "Periodic", r: (r) => (r.periodic ? "yes" : "no") }]} rows={s.jobs} /></Card></div> },
          { id: "s4", label: "S/4HANA data-model impacts", content: <Card><Table cols={[{ k: "table", h: "Table" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "rows", h: "Rows" }, { k: "note", h: "Simplification note" }]} rows={s.s4_impacts} /></Card> },
          { id: "est", label: "Estimates", content: <Card><KV obj={s.estimates} /><p className="muted">{s.estimates.disclaimer}</p></Card> },
        ]} />
      </>}
    </div>
  );
}

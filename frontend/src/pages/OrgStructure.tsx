import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Table } from "../components/ui";

function Node({ u }: { u: any }) {
  return <li><span className="code">{u.code}</span>{u.name} <span className="muted">({u.type.toLowerCase().replace("_", " ")}{u.attributes?.WAERS ? `, ${u.attributes.WAERS}` : ""}{u.attributes?.LAND1 ? `, ${u.attributes.LAND1}` : ""})</span>{u.children?.length > 0 && <ul>{u.children.map((c: any) => <Node key={c.type + c.code} u={c} />)}</ul>}</li>;
}

export default function OrgStructure() {
  const { source } = useProjectDetails();
  const q = useApi<any>(source ? `/systems/${source.id}/org-structure` : null, undefined, [source?.id]);
  if (!source) return <Banner>Select a project with a source system.</Banner>;
  if (q.data && !q.data.units.length) return <Banner>Run discovery first.</Banner>;
  return (
    <div className="grid2">
      <Card title="Enterprise hierarchy (controlling area → company code → plants / orgs / cost objects)"><ErrorBox error={q.error} /><div className="tree"><ul>{(q.data?.tree || []).map((a: any) => <Node key={a.code} u={a} />)}</ul></div></Card>
      <Card title="Organisational units"><Table cols={[{ k: "type", h: "Type" }, { k: "code", h: "Code" }, { k: "name", h: "Name" }, { k: "parent_code", h: "Parent" }]} rows={(q.data?.units || []).filter((u: any) => !["COST_CENTER", "PROFIT_CENTER"].includes(u.type))} /><p className="muted">{Object.entries(q.data?.counts || {}).map(([t, n]) => `${t}: ${n}`).join(" · ")}</p></Card>
    </div>
  );
}

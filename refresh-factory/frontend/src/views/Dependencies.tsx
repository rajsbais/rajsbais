import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable } from "../components";
import { useApp } from "../ctx";

const MODCOL: Record<string, number> = { CONFIG: 0, MD: 1, SD: 2, MM: 2, FI: 3, CUSTOM: 3 };

function TypeGraph({ g }: { g: J }) {
  const cols: Record<number, J[]> = {};
  g.nodes.forEach((n: J) => { (cols[MODCOL[n.module] ?? 2] ??= []).push(n); });
  const pos: Record<string, { x: number; y: number }> = {};
  Object.entries(cols).forEach(([c, ns]) => (ns as J[]).forEach((n, i) => { pos[n.id] = { x: 70 + Number(c) * 210, y: 40 + i * 62 }; }));
  const h = Math.max(...Object.values(pos).map((p) => p.y)) + 50;
  return (
    <svg viewBox={`0 0 900 ${h}`} className="graph" role="img" aria-label="Business object type dependency graph">
      <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="currentColor" /></marker></defs>
      {g.edges.map((e: J, i: number) => {
        const a = pos[e.from], b = pos[e.to]; if (!a || !b) return null;
        return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={e.kind === "config" ? "edge cfg" : "edge"} markerEnd="url(#arr)"><title>{e.name} via {e.via}</title></line>;
      })}
      {g.nodes.map((n: J) => (
        <g key={n.id} transform={`translate(${pos[n.id].x - 75},${pos[n.id].y - 16})`}>
          <rect width="150" height="32" rx="6" className={`node ${n.kind}`} />
          <text x="75" y="20" textAnchor="middle">{n.label}</text><title>{n.tables.join(", ")}</title>
        </g>))}
    </svg>
  );
}

export default function Dependencies() {
  const { project } = useApp();
  const [g, setG] = useState<J>(null);
  const [inst, setInst] = useState<J>(null);
  const [type, setType] = useState(""); const [origin, setOrigin] = useState("");
  const fam = project?.source?.family ?? "ECC";
  useEffect(() => { api.get(`/api/objects/registry?family=${fam}`).then(setG).catch(() => undefined); }, [fam]);
  useEffect(() => {
    setInst(null);
    if (project?.has_plan) api.get(`/api/projects/${project.id}/instances?limit=500${type ? `&type=${type}` : ""}${origin ? `&origin=${origin}` : ""}`).then(setInst).catch(() => undefined);
  }, [project?.id, project?.has_plan, project?.status, type, origin]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <Card title={`Business object model — ${fam === "S4" ? "S/4HANA (Business Partner, ACDOCA)" : "ECC"} · semantic relationships, not just foreign keys`}>
        {g && <><TypeGraph g={g} /><p>{g.validation.ok ? <Badge kind="ok">no cycles · registry valid</Badge> : <Badge kind="bad">invalid: {JSON.stringify(g.validation)}</Badge>}
          <span className="muted small"> Solid = requires (copied) · dashed = customizing prerequisite (verified in target, never copied)</span></p></>}
      </Card>
      <Card title="Expanded scope for the active project">
        {!project?.has_plan ? <p className="muted">Build a plan in the Selective Refresh Designer to explore instances.</p> : (
          <>
            <div className="row">
              <select value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by type"><option value="">all types</option>
                {g?.nodes.filter((n: J) => n.kind !== "config").map((n: J) => <option key={n.id}>{n.id}</option>)}</select>
              <select value={origin} onChange={(e) => setOrigin(e.target.value)} aria-label="Filter by origin"><option value="">all origins</option><option>ROOT</option><option>REQUIRED</option><option>DOWNSTREAM</option></select>
            </div>
            <DataTable rows={inst?.items ?? []} cols={[
              { key: "id", title: "Object" }, { key: "origin", title: "Origin" }, { key: "parent", title: "Pulled in by", render: (r) => r.parent ?? "—" },
              { key: "requires", title: "Requires", render: (r) => r.requires.length, sortValue: (r) => r.requires.length },
              { key: "configs", title: "Customizing", render: (r) => r.configs.join(", ") || "—" },
              { key: "rows", title: "Rows", render: (r) => Object.values(r.rows as Record<string, number>).reduce((a, b) => a + b, 0), sortValue: (r) => Object.values(r.rows as Record<string, number>).reduce((a, b) => a + b, 0) },
              { key: "decision", title: "Decision", render: (r) => r.decision ?? "—" },
            ]} />
          </>)}
      </Card>
    </>
  );
}

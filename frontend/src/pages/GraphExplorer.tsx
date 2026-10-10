import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, List, Pill, Table } from "../components/ui";

const COLORS: Record<string, string> = { "SD.SalesOrder": "#4f8cff", "SD.Delivery": "#58a6ff", "SD.BillingDocument": "#79c0ff", "FI.AccountingDocument": "#3fb950", "MM.PurchaseOrder": "#d29922", "MM.MaterialDocument": "#e3b341", "MM.InvoiceReceipt": "#f0c674", "MD.Customer": "#bc8cff", "MD.Vendor": "#d2a8ff", "MD.Material": "#ff7b72", "CFG.Plant": "#8b98a5", "CFG.CompanyCode": "#c9d1d9", "FI.GLAccount": "#56d364", "CO.CostCenter": "#7ee787", "CO.ProfitCenter": "#a5d6ff", "PP.ProductionOrder": "#ffa657" };

function GraphSvg({ nodes, edges, center, onPick }: { nodes: any[]; edges: any[]; center: string; onPick: (id: string) => void }) {
  const layout = useMemo(() => {
    const types = Array.from(new Set(nodes.map((n) => n.type))).sort((a, b) => (a === nodes.find((n) => n.id === center)?.type ? -1 : b === nodes.find((n) => n.id === center)?.type ? 1 : a.localeCompare(b)));
    const colW = 1000 / Math.max(1, types.length);
    const pos: Record<string, { x: number; y: number }> = {};
    types.forEach((t, ci) => { const ns = nodes.filter((n) => n.type === t); ns.forEach((n, ri) => { pos[n.id] = { x: 60 + ci * colW + colW / 2, y: 30 + ((ri + 0.5) * 520) / Math.max(1, ns.length) }; }); });
    return { pos, types };
  }, [nodes, edges, center]);
  return (
    <>
      <svg className="graph" viewBox="0 0 1120 560">
        {edges.map((e, i) => { const a = layout.pos[e.from]; const b = layout.pos[e.to]; return a && b ? <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={e.type === "CROSS_COMPANY" ? "#f85149" : undefined} strokeDasharray={e.type === "MASTER_REF" ? "3 3" : undefined} /> : null; })}
        {nodes.map((n) => { const p = layout.pos[n.id]; if (!p) return null; const cross = n.attributes?.cross_company || n.attributes?.shared; return <g key={n.id} className="n" onClick={() => onPick(n.id)}><circle cx={p.x} cy={p.y} r={n.id === center ? 9 : 6} fill={COLORS[n.type] || "#8b98a5"} stroke={cross ? "#f85149" : "none"} strokeWidth={2} /><text x={p.x + 10} y={p.y + 3}>{n.id.split(":")[1]?.slice(0, 22)}</text></g>; })}
      </svg>
      <div className="legend">{layout.types.map((t) => <span key={t} style={{ ["--c" as any]: COLORS[t] || "#8b98a5" }}>{t}</span>)}<span style={{ ["--c" as any]: "#f85149" }}>red ring = cross-company / shared</span></div>
    </>
  );
}

export default function GraphExplorer() {
  const { source } = useProjectDetails();
  const stats = useApi<any>(source ? `/systems/${source.id}/graph/stats` : null, undefined, [source?.id]);
  const [q, setQ] = useState("");
  const [type, setType] = useState("SD.SalesOrder");
  const [node, setNode] = useState<string | null>(null);
  const [depth, setDepth] = useState(2);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [trav, setTrav] = useState<any>(null);
  const search = useApi<any[]>(source ? `/systems/${source.id}/graph/nodes` : null, { node_type: type, q, limit: 30 }, [source?.id]);
  const nb = useApi<any>(source && node ? `/systems/${source.id}/graph/neighbourhood` : null, { node, depth }, [node, depth]);
  const build = async () => { setBusy(true); setErr(null); try { await api(`/systems/${source.id}/graph/build`, { method: "POST" }); stats.reload(); search.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const traverse = async () => { if (!node) return; try { setTrav(await api(`/systems/${source.id}/graph/traverse`, { body: { seeds: [node], max_depth: 6 } })); } catch (e: any) { setErr(e.message); } };
  const org = useApi<any>(source ? `/systems/${source.id}/org-structure` : null, undefined, [source?.id]);
  const ccs: any[] = (org.data?.units || []).filter((u: any) => u.type === "COMPANY_CODE");
  const [cc, setCc] = useState("");
  useEffect(() => { if (!cc && ccs.length) setCc(ccs.find((u) => u.code !== ccs[0].code)?.code || ccs[0].code); }, [ccs.length]);
  const ccNb = useApi<any>(source && cc && stats.data?.nodes ? `/systems/${source.id}/graph/neighbourhood` : null, { node: `CFG.CompanyCode:${cc}`, depth: 2 }, [cc, stats.data?.nodes]);
  const related: any[] = (ccNb.data?.nodes || []).filter((n: any) => n.id !== `CFG.CompanyCode:${cc}` && !n.id.startsWith("CFG."));
  const flagged = related.filter((n) => n.attributes?.shared || n.attributes?.cross_company);
  if (!source) return <Banner>Select a project with a source system.</Banner>;
  return (
    <div>
      <div className="grid2">
        <Card><label className="field">Company code<select value={cc} onChange={(e) => setCc(e.target.value)} disabled={!ccs.length}>{ccs.map((u) => <option key={u.code} value={u.code}>{u.code} — {u.name}</option>)}{!ccs.length && <option value="">run discovery first</option>}</select></label><p style={{ margin: 0 }}>Related nodes: <b>{ccNb.data ? related.length : stats.data && !stats.data.nodes ? "graph not built" : "…"}</b>{ccNb.data?.truncated ? " (truncated)" : ""}</p></Card>
        <Card title="Shared and cross-company"><List items={flagged.slice(0, 12).map((n) => ({ id: n.id, k: n.id, d: [n.attributes?.shared ? "shared" : "", n.attributes?.cross_company ? "cross-company" : ""].filter(Boolean).join(" · ") }))} empty={ccNb.data ? "Nothing shared or cross-company within two hops of this company code" : "…"} onPick={setNode} /></Card>
      </div>
      <Card title="Related objects"><List items={related.slice(0, 20).map((n) => ({ id: n.id, k: n.id, d: n.attributes?.company_codes?.length ? `company codes ${n.attributes.company_codes.join(", ")}` : undefined }))} empty={ccNb.data ? "No objects within two hops of this company code" : stats.data && !stats.data.nodes ? "Build the graph first" : "…"} onPick={setNode} />{related.length > 20 && <p className="muted">{related.length - 20} more; pick one to open its neighbourhood below.</p>}</Card>
      <Card title="Dependency graph" actions={<button disabled={busy} onClick={build}>{busy ? "Building…" : "Build / rebuild graph"}</button>}>
        <ErrorBox error={err || stats.error} />
        {stats.data && <p>{stats.data.nodes.toLocaleString()} nodes · {stats.data.edges.toLocaleString()} edges · {Object.entries(stats.data.edges_by_type).map(([t, n]) => `${t}: ${n}`).join(" · ")}</p>}
        {stats.data && stats.data.nodes === 0 && <Banner>Graph not built yet. Discovery must have run first.</Banner>}
        <div className="row"><select value={type} onChange={(e) => setType(e.target.value)}>{Object.keys(stats.data?.nodes_by_type || {}).sort().map((t) => <option key={t}>{t}</option>)}</select><input placeholder="search key…" value={q} onChange={(e) => setQ(e.target.value)} /><label className="chk">Depth <input type="number" min={1} max={4} value={depth} onChange={(e) => setDepth(Number(e.target.value))} style={{ width: 60 }} /></label><button className="secondary" disabled={!node} onClick={traverse}>Traverse for selective extraction</button></div>
        <div className="row">{(search.data || []).slice(0, 30).map((n) => <button key={n.id} className="secondary" onClick={() => setNode(n.id)}>{n.id.split(":")[1]} {n.attributes?.cross_company && <Pill value="cross-company" />}{n.attributes?.shared && <Pill value="shared" />}</button>)}</div>
      </Card>
      {node && nb.data && <Card title={`Neighbourhood of ${node} (depth ${depth})`}><GraphSvg nodes={nb.data.nodes} edges={nb.data.edges} center={node} onPick={setNode} /></Card>}
      {trav && <div className="grid2"><Card title={`Selective extraction set (${Object.keys(trav.included).length} objects)`}><Table cols={[{ k: "node", h: "Object" }, { k: "inc", h: "Inclusion", r: (r) => <Pill value={r.inc} /> }]} rows={Object.entries(trav.included).map(([node, inc]) => ({ node, inc }))} /></Card><Card title="Explanations (why each object was added)"><Table cols={[{ k: "node", h: "Object" }, { k: "depth", h: "Depth" }, { k: "edge_name", h: "Via" }, { k: "policy", h: "Policy", r: (r) => <Pill value={r.policy} /> }, { k: "reason", h: "Reason" }]} rows={trav.traces} /></Card></div>}
      <Card title="Relationship model (type level)"><Table cols={[{ k: "from", h: "From" }, { k: "name", h: "Relationship" }, { k: "to", h: "To" }, { k: "edge", h: "Edge type" }, { k: "description", h: "Resolved from" }]} rows={stats.data?.relationship_model || []} /></Card>
    </div>
  );
}

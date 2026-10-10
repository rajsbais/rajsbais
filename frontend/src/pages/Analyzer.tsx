import { useState } from "react";
import { fmtBytes, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Bars, Card, ErrorBox, Pill, Table, Tabs } from "../components/ui";
import { ProcessAnalysis } from "../components/ProcessAnalysis";

export default function Analyzer() {
  const { source } = useProjectDetails();
  const stats = useApi<any[]>(source ? `/systems/${source.id}/table-statistics` : null, undefined, [source?.id]);
  const disc = useApi<any>(source ? `/systems/${source.id}/discovery` : null, undefined, [source?.id]);
  const [table, setTable] = useState<string>("BSEG");
  const sel = (stats.data || []).find((t) => t.table === table);
  const inv = disc.data?.summary?.business_objects || {};
  const byCc: Record<string, number> = {};
  (stats.data || []).forEach((t) => Object.entries(t.by_company_code || {}).forEach(([cc, n]) => { byCc[cc] = (byCc[cc] || 0) + (n as number); }));
  if (!source) return <Banner>Select a project with a source system.</Banner>;
  if (stats.error || !stats.data?.length) return <Banner>Run discovery in the Landscape Explorer first.</Banner>;
  return (
    <div>
      <ErrorBox error={stats.error} />
      <Tabs tabs={[
        { id: "bpa", label: "Business process analysis", content: <ProcessAnalysis systemId={source.id} /> },
        { id: "dist", label: "Data distribution", content: <div className="grid2"><Card title="Rows by company code (all tables)"><Bars data={Object.entries(byCc).sort().map(([l, v]) => ({ label: l, value: v }))} /></Card><Card title={`Table ${table}`} actions={<select value={table} onChange={(e) => setTable(e.target.value)}>{(stats.data || []).map((t) => <option key={t.table} value={t.table}>{t.table} ({fmtNum(t.rows)})</option>)}</select>}>{sel && <><p className="muted">{sel.description} · keys: {sel.key_fields.join(", ")} · <Pill value={sel.s4_status} /> {sel.s4_note}</p><Bars data={Object.entries(sel.by_company_code).map(([l, v]) => ({ label: `CC ${l}`, value: v as number }))} />{Object.keys(sel.by_fiscal_year).length > 0 && <><h4>By fiscal year</h4><Bars data={Object.entries(sel.by_fiscal_year).sort().map(([l, v]) => ({ label: l, value: v as number }))} /></>}</>}</Card></div> },
        { id: "obj", label: "Business object volumes", content: <Card><Table cols={[{ k: "name", h: "Object" }, { k: "domain", h: "Domain" }, { k: "kind", h: "Kind" }, { k: "count", h: "Instances", r: (r) => fmtNum(r.count) }, { k: "open", h: "Open" }, { k: "shared", h: "Shared / cross-company" }, { k: "by_company_code", h: "By company code", r: (r) => Object.entries(r.by_company_code).map(([c, n]) => `${c}:${n}`).join("  ") }, { k: "by_year", h: "By year", r: (r) => Object.entries(r.by_year).map(([c, n]) => `${c}:${n}`).join("  ") }]} rows={Object.values(inv)} /></Card> },
        { id: "cplx", label: "Complexity & scope estimate", content: <div className="grid2"><Card title="Complexity factors"><Bars data={Object.entries(disc.data?.summary?.complexity?.factors || {}).map(([l, v]) => ({ label: l, value: v as number }))} /><p>Band: <Pill value={disc.data?.summary?.complexity?.band} /> score {disc.data?.summary?.complexity?.score}</p></Card><Card title="Estimated migration scope"><p>Total size {fmtBytes(disc.data?.summary?.estimates?.est_bytes)} · {fmtNum(disc.data?.summary?.estimates?.est_business_objects)} business objects</p><p>Estimated full-extract duration: {disc.data?.summary?.estimates?.est_full_extract_seconds}s at {fmtNum(disc.data?.summary?.estimates?.assumed_throughput_rec_s)} rec/s (placeholder)</p><p className="muted">{disc.data?.summary?.estimates?.disclaimer}</p></Card></div> },
        { id: "tbl", label: "All tables", content: <Card><Table cols={[{ k: "table", h: "Table" }, { k: "description", h: "Description" }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "est_bytes", h: "Size", r: (r) => fmtBytes(r.est_bytes) }, { k: "custom", h: "Custom", r: (r) => (r.custom ? "Z/Y" : "") }, { k: "s4_status", h: "S/4", r: (r) => <Pill value={r.s4_status} /> }]} rows={stats.data} /></Card> },
      ]} />
    </div>
  );
}

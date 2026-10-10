import { useState } from "react";
import { fmtNum } from "../api";
import { useApi } from "../hooks";
import { Bars, Card, ErrorBox, Pill, Segmented, Table } from "./ui";

const AREAS = [{ value: "ALL", label: "All processes" }, { value: "O2C", label: "Order to cash" }, { value: "P2P", label: "Purchase to pay" }, { value: "R2R", label: "Record to report" }];
const pct = (x: number) => `${(100 * (x || 0)).toFixed(1)} %`;

/** Business process analysis from the database footprint of the source, computed in the system through the
 *  add-on's aggregate module: variants (TAANA), selectivity (DB05), age, growth (DB02), links (DB15). */
export function ProcessAnalysis({ systemId }: { systemId: string }) {
  const [area, setArea] = useState("ALL");
  const [bukrs, setBukrs] = useState("");
  const [years, setYears] = useState(7);
  const q = useApi<any>(`/systems/${systemId}/process-analysis`, { area, bukrs: bukrs || undefined, top: 8, retention_years: years }, [systemId, area, bukrs, years]);
  const d = q.data;
  return (
    <div>
      <Segmented value={area} onChange={setArea} options={AREAS} />
      <div className="row"><label className="chk">Company codes <input value={bukrs} onChange={(e) => setBukrs(e.target.value)} placeholder="all (e.g. 5000,1000)" style={{ width: 200 }} /></label><label className="chk">Retention horizon <input type="number" min={0} max={50} value={years} onChange={(e) => setYears(Number(e.target.value))} style={{ width: 70 }} /> years</label>{d && <span className="muted">analysed through {d.transport || "no transport"}{d.rfc_calls ? ` · ${d.rfc_calls} aggregate calls` : ""}{d.snapshot ? ` · snapshot ${d.snapshot}` : ""}</span>}</div>
      <ErrorBox error={q.error || d?.errors?.connection} />
      {q.loading && !d && <p className="muted">Aggregating in the source…</p>}
      {d && <>
        <Card title="What the analysis answers">
          <Table cols={[{ k: "objective", h: "Objective" }, { k: "transactions", h: "Basis transactions" }, { k: "here", h: "Here" }]} rows={d.matrix} />
        </Card>
        <Card title="Process variants (TAANA-style distributions)">
          <p className="muted">Each table grouped by its type fields, counted in the system; the 80 % line says how many variants carry most of the volume. A long tail of rarely used variants is a candidate for harmonisation before the move.</p>
          <div className="grid2">{d.variants.map((v: any) => <div key={`${v.table}:${v.fields.join("+")}`}><h4><span className="mono">{v.table}</span> by {v.fields.join(" + ")} <span className="muted">· {v.meaning}</span></h4><p className="muted">{fmtNum(v.total)} rows · {v.variants} variants · <b>{v.pareto_variants}</b> make 80 % of the volume{v.pushdown ? " · company codes pushed down" : ""}</p><Bars data={v.top.map((t: any) => ({ label: t.variant, value: t.count, hint: `${pct(t.share)} (cumulative ${pct(t.cumulative)})` }))} /></div>)}</div>
        </Card>
        <div className="grid2">
          <Card title="Selectivity (DB05-style)">
            <Table cols={[{ k: "table", h: "Table", r: (r) => <span className="mono">{r.table}</span> }, { k: "field", h: "Field", r: (r) => <span className="mono">{r.field}</span> }, { k: "meaning", h: "Meaning" }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "distinct", h: "Distinct", r: (r) => fmtNum(r.distinct) }, { k: "rows_per_value", h: "Rows / value" }, { k: "top_value", h: "Top value", r: (r) => <span className="mono">{r.top_value}</span> }, { k: "top_share", h: "Top share", r: (r) => pct(r.top_share) }]} rows={d.selectivity} empty="No selectivity row for this area" />
          </Card>
          <Card title="Age profile and archiving horizon">
            <Table cols={[{ k: "table", h: "Table", r: (r) => <span className="mono">{r.table}</span> }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "older_than_horizon", h: `Older than ${d.age[0]?.horizon_year ?? "horizon"}`, r: (r) => fmtNum(r.older_than_horizon) }, { k: "archivable_share", h: "Archivable share", r: (r) => <Pill value={r.archivable_share >= 0.5 ? "HIGH" : r.archivable_share >= 0.2 ? "MEDIUM" : "LOW"} /> }, { k: "by_year", h: "By year", r: (r) => Object.entries(r.by_year).map(([y, n]) => `${y}: ${n}`).join("  ") }]} rows={d.age} empty="No age profile for this area" />
            <p className="muted">Documents older than the retention horizon are candidates for archiving before the move, or for a historical policy of open items and balances only.</p>
          </Card>
        </div>
        <div className="grid2">
          <Card title="Growth (DB02-style, from the discovery statistics)">
            <Table cols={[{ k: "table", h: "Table", r: (r) => <span className="mono">{r.table}</span> }, { k: "rows", h: "Rows", r: (r) => fmtNum(r.rows) }, { k: "latest_year", h: "Latest year" }, { k: "latest_rows", h: "Rows", r: (r) => fmtNum(r.latest_rows) }, { k: "change", h: "vs previous", r: (r) => (r.change === null || r.change === undefined ? "-" : `${r.change >= 0 ? "+" : ""}${(100 * r.change).toFixed(0)} %`) }, { k: "objects", h: "Business objects (DB15)", r: (r) => r.objects.join(", ") }]} rows={d.growth.slice(0, 15)} empty="Run discovery first" />
          </Card>
          <Card title="Usage and workflow (not available through the add-on)">
            <p><Pill value={d.usage.status} /> {d.usage.transactions.join(", ")}</p>
            <p className="muted">{d.usage.reason}</p>
            {Object.keys(d.errors || {}).length > 0 && <><h4>Not readable</h4><ul className="muted">{Object.entries(d.errors).map(([k, v]: any) => <li key={k}><span className="mono">{k}</span>: {v}</li>)}</ul></>}
          </Card>
        </div>
      </>}
    </div>
  );
}

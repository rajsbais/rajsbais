import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, Stat, StatusBadge } from "../components";
import { useApp } from "../ctx";

export default function Reconciliation() {
  const app = useApp();
  const [rec, setRec] = useState<J>(null);
  const [cat, setCat] = useState("");
  useEffect(() => { setRec(null); if (app.runId) api.get(`/api/runs/${app.runId}/reconciliation`).then(setRec).catch(() => setRec(null)); }, [app.runId]);
  if (!app.runId || !rec) return <Card><p className="muted">No reconciliation available. Complete a run first.</p></Card>;
  const rows = rec.checks.filter((c: J) => !cat || c.category === cat);
  return (
    <>
      <Card title="Release gate" actions={<>
        <button onClick={() => api.text(`/api/runs/${app.runId}/report`).then((t) => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([t], { type: "text/markdown" })); a.download = `${app.runId}-report.md`; a.click(); })}>Download report</button>
        <button disabled={!app.can("audit:read")} title={app.can("audit:read") ? "" : "requires audit:read"} onClick={() => api.download(`/api/runs/${app.runId}/evidence`, `${app.runId}-evidence.zip`)}>Evidence package</button></>}>
        <p>{rec.release === "RELEASED" ? <Badge kind="ok">RELEASED</Badge> : <Badge kind="bad">HELD</Badge>} <span className="muted">{rec.release_rule}</span></p>
        {rec.failed.length > 0 && <p>Failed checks: {rec.failed.join(", ")}</p>}
      </Card>
      <div className="grid stats">
        {Object.entries(rec.summary).map(([k, v]: [string, J]) => <Stat key={k} label={k} value={`${v.pass} ✓  ${v.fail} ✗`} />)}
      </div>
      <Card title="Checks" actions={<select value={cat} onChange={(e) => setCat(e.target.value)} aria-label="Category"><option value="">all</option><option>technical</option><option>business</option><option>security</option></select>}>
        <DataTable rows={rows} cols={[
          { key: "category", title: "Category" }, { key: "id", title: "Check" }, { key: "name", title: "What is verified" },
          { key: "status", title: "Result", render: (c) => <StatusBadge s={c.status} /> }, { key: "detail", title: "Detail" },
          { key: "samples", title: "Examples", render: (c) => (c.samples.length ? c.samples.slice(0, 3).join(" · ") : "") }]} pageSize={15} />
      </Card>
      <Card title="Masking disclosure">
        <p>Protection class: {rec.masking.protection_classes.map((c: string) => <Badge key={c} kind="warn">{c}</Badge>)} · reversible tokens: {String(rec.masking.reversible)}</p>
        <p className="muted small">{rec.masking.residual_risk_note}</p>
      </Card>
    </>
  );
}

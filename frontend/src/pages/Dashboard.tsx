import { useEffect, useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Hero, Pill, Stat, Table, Tile, isSimulated } from "../components/ui";
import { crossCompany, dbSize, sharedRisk } from "../lib";
import { JourneyStrip } from "./Journey";

/** Executive dashboard: the landscape in four figures, a quick carve-out assessment per company code, the
 *  recent runs and the capability status. Every figure comes from the discovery snapshot and the scope engine. */
export default function Dashboard() {
  const { projectId, project, source, target } = useProjectDetails();
  const caps = useApi<any[]>("/platform/capabilities");
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const disc = useApi<any>(source ? `/systems/${source.id}/discovery` : null, undefined, [source?.id]);
  const org = useApi<any>(source ? `/systems/${source.id}/org-structure` : null, undefined, [source?.id]);
  const objs = useApi<any>(source ? `/systems/${source.id}/business-objects` : null, { limit: 1 }, [source?.id]);
  const s = disc.data?.summary;
  const ccs: any[] = (org.data?.units || []).filter((u: any) => u.type === "COMPANY_CODE");
  const [cc, setCc] = useState("");
  const [quick, setQuick] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (!cc && ccs.length) setCc(ccs.find((u) => u.code !== ccs[0].code)?.code || ccs[0].code); }, [ccs.length]);
  useEffect(() => {
    if (!cc || !source || !target || !projectId) return;
    let alive = true;
    setBusy(true); setErr(null);
    api(`/projects/${projectId}/scopes/evaluate`, { body: { name: `Quick carve-out ${cc}`, source_system_id: source.id, target_system_id: target.id, company_codes: [cc], shared_object_policy: "REFERENCE", cross_company_policy: "INCLUDE_FLAG" } })
      .then((r) => { if (alive) setQuick(r); }).catch((e) => { if (alive) setErr(e.message); }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [cc, source?.id, target?.id, projectId]);
  if (!project) return <Banner>Select or create a project first (Dashboard → Portfolio).</Banner>;
  const sample = Object.values(objs.data?.counts || {}).reduce((a: number, b: any) => a + Number(b || 0), 0);
  const size = dbSize(s?.tables?.est_bytes);
  const risk = sharedRisk(quick?.impact?.by_classification);
  const lastRun = (runs.data || [])[0];
  return (
    <div>
      <Hero title="Executive dashboard" subtitle={s ? `${s.system.product} ${s.system.release} · ${size.value} ${size.unit} ${isSimulated(source) ? "illustrative" : "discovered"} source` : `${project.name} · run discovery on the source to size the landscape`} />
      <ErrorBox error={runs.error || disc.error} />
      <JourneyStrip />
      <div className="stats two">
        <Stat label="Company codes" value={s?.org_units?.COMPANY_CODE ?? ccs.length ?? "-"} />
        <Stat label="Plants" value={s?.org_units?.PLANT ?? "-"} />
        <Stat label="Sample objects" value={sample ? fmtNum(sample) : "-"} sub={objs.data ? `${Object.keys(objs.data.counts || {}).length} object types discovered` : undefined} />
        <Stat label={`${size.unit} database`} value={size.value} sub={s ? `${fmtNum(s.tables.total_rows)} rows in ${s.tables.count} tables` : undefined} />
      </div>
      <Card title="Quick carve-out">
        <label className="field">Company code<select value={cc} onChange={(e) => setCc(e.target.value)} disabled={!ccs.length}>{ccs.map((u) => <option key={u.code} value={u.code}>{u.code} — {u.name}</option>)}{!ccs.length && <option value="">run discovery first</option>}</select></label>
        <ErrorBox error={err} />
        <div className="tiles">
          <Tile label="Related objects" value={busy ? "…" : quick ? fmtNum(quick.impact.objects_total) : "-"} />
          <Tile label="Shared risk" value={busy ? "…" : quick ? risk.band : "-"} tone={quick ? (risk.band === "LOW" ? "good" : risk.band === "MEDIUM" ? "warn" : "bad") : undefined} />
          <Tile label="Cross-company" value={busy ? "…" : quick ? fmtNum(crossCompany(quick.impact.by_classification)) : "-"} />
        </div>
        {quick && <p className="muted" style={{ marginTop: 12 }}>Scope evaluated by the engine for company code {cc}: {fmtNum(quick.impact.objects_seeded)} seeded objects, {fmtNum(quick.impact.objects_expanded)} added by dependency traversal, {fmtNum(quick.impact.approvals_required)} needing a business disposition; shared risk is the share of objects that are shared, referenced or need a manual disposition ({risk.shared} of {risk.total}). Generate the immutable manifest in the Carve-out studio.</p>}
      </Card>
      <div className="grid2">
        <Card title="Recent runs"><Table cols={[{ k: "id", h: "Run", r: (r) => <span className="mono">{r.id.slice(0, 8)}</span> }, { k: "mode", h: "Mode", r: (r) => <Pill value={r.mode} /> }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "reconciliation", h: "Reconciliation", r: (r) => <Pill value={r.reconciliation} /> }, { k: "started_at", h: "Started", r: (r) => String(r.started_at || "").slice(0, 16) }]} rows={(runs.data || []).slice(0, 6)} empty="No runs yet: start one under Execute → Runs." />{lastRun && <p className="muted">Latest run {lastRun.id.slice(0, 8)}: {lastRun.status}, reconciliation {lastRun.reconciliation || "pending"}. The Audit tab holds the verified report.</p>}</Card>
        <Card title="Capability status"><Table cols={[{ k: "area", h: "Area" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "note", h: "Note" }]} rows={(caps.data || []).slice(0, 12)} /><p className="muted">Full list under /platform/capabilities and docs/capability-status.md.</p></Card>
      </div>
    </div>
  );
}

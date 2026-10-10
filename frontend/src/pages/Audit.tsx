import { useState } from "react";
import { fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Facts, Hero, Pill, Select, Table, Tile } from "../components/ui";
import { auditVerdict, reconciliationStatus, sharedRisk } from "../lib";

/** Audit report: the verified vertical slice of one run, from its evidence package and reconciliation. */
export default function Audit() {
  const { projectId } = useProjectDetails();
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const [sel, setSel] = useState("");
  const id = sel || runs.data?.[0]?.id;
  const run = (runs.data || []).find((r) => r.id === id);
  const rep = useApi<any>(id ? `/runs/${id}/report` : null, undefined, [id]);
  const tech = useApi<any[]>(id ? `/runs/${id}/reconciliation` : null, { layer: "TECHNICAL", limit: 1000 }, [id]);
  const ev = useApi<any>(id ? `/runs/${id}/evidence` : null, undefined, [id]);
  const org = useApi<any>(run?.source_system_id ? `/systems/${run.source_system_id}/org-structure` : null, undefined, [run?.source_system_id]);
  if (!projectId) return <Banner>Select a project.</Banner>;
  if (runs.data && !runs.data.length) return <><Hero title="Audit report" subtitle="No run to report on yet" /><Banner>Start a run under Execute → Runs; the audit report is built from its evidence package.</Banner></>;
  const r = rep.data || {};
  const m = r.manifest || {};
  const ccs: string[] = m.company_codes || [];
  const names = Object.fromEntries(((org.data?.units || []) as any[]).filter((u) => u.type === "COMPANY_CODE").map((u) => [u.code, u.name]));
  const counts = (tech.data || []).filter((x) => x.check === "record_count");
  const src = counts.reduce((a, x) => a + (Number(x.source) || 0), 0);
  const tgt = counts.reduce((a, x) => a + (Number(x.target) || 0), 0);
  const matched = counts.filter((x) => x.status === "PASS").reduce((a, x) => a + (Number(x.target) || 0), 0);
  const verdict = auditVerdict(run);
  const rs = reconciliationStatus(r.reconciliation?.overall || run?.reconciliation);
  const risk = sharedRisk(m.impact?.by_classification);
  const when = run?.finished_at || run?.started_at;
  return (
    <div>
      <Hero title="Audit report" subtitle={ccs.length ? `Verified vertical slice for company code${ccs.length > 1 ? "s" : ""} ${ccs.join(", ")}` : "Verified vertical slice"} />
      <div className="row"><Select value={id} onChange={setSel} options={(runs.data || []).map((x) => ({ value: x.id, label: `${x.id.slice(0, 8)} ${x.status} ${String(x.started_at || "").slice(0, 16)}` }))} /></div>
      <ErrorBox error={rep.error || tech.error} />
      <Card>
        <p><Pill value={verdict} /></p>
        <Facts rows={[
          { k: "Timestamp", v: when ? new Date(when).toISOString().replace(/\.\d+Z$/, "Z") : "-" },
          { k: ccs.length > 1 ? "Company codes" : "Company code", v: ccs.map((c) => `${c}${names[c] ? ` — ${names[c]}` : ""}`).join(", ") || "-" },
          { k: "Related objects", v: m.impact ? fmtNum(m.impact.objects_total) : "-" },
          { k: "Manifest hash", v: m.hash ? m.hash.slice(0, 16) : "-", mono: true },
          { k: "Shared exposure", v: m.impact ? <Pill value={risk.band} /> : "-" },
          { k: "Ruleset", v: r.ruleset ? `${r.ruleset.name} v${r.ruleset.version}${r.ruleset.approved_by ? ` · approved by ${r.ruleset.approved_by}` : ""}` : "-" },
        ]} />
      </Card>
      <div className="tiles" style={{ marginTop: 0, marginBottom: 16 }}>
        <Tile label="Source" value={counts.length ? fmtNum(src) : "-"} />
        <Tile label="Target" value={counts.length ? fmtNum(tgt) : "-"} />
        <Tile label="Matched" value={counts.length ? fmtNum(matched) : "-"} tone={counts.length && matched === src ? "good" : undefined} />
      </div>
      <p className={`status-line ${rs.cls}`}>Reconciliation status: {rs.label}</p>
      <p className="muted">Source and target are the record counts of the {counts.length} tables the technical layer compared; matched counts the rows of the tables whose counts agree. {r.reconciliation ? `${r.reconciliation.checks ?? ""} checks: ${Object.entries(r.reconciliation.by_layer || {}).map(([l, c]: any) => `${l} ${c.PASS || 0} pass / ${c.WARN || 0} warn / ${c.FAIL || 0} fail`).join(" · ")}.` : ""} {r.disclaimer || ""}</p>
      <div className="grid2">
        <Card title="Evidence package"><Table cols={[{ k: "name", h: "File", r: (x) => <span className="mono">{x.name}</span> }, { k: "sha256", h: "SHA-256", r: (x) => <span className="mono">{String(x.sha256).slice(0, 16)}…</span> }, { k: "bytes", h: "Bytes", r: (x) => fmtNum(x.bytes) }]} rows={Object.entries(ev.data?.evidence?.files || {}).map(([name, v]: any) => ({ name, ...v }))} empty="No evidence package yet" /></Card>
        <Card title="Approvals (four-eyes)"><Table cols={[{ k: "subject_type", h: "Subject" }, { k: "kind", h: "Kind" }, { k: "decision", h: "Decision", r: (x) => <Pill value={x.decision} /> }, { k: "decided_by", h: "By" }]} rows={ev.data?.approvals} empty="No approvals recorded" /><p className="muted">The hash-chained audit trail and the compliance findings are under Audit → Compliance and evidence.</p></Card>
      </div>
      {r.reconciliation?.failures?.length > 0 && <Card title={`Open findings (${r.reconciliation.failures.length})`}><Table cols={[{ k: "layer", h: "Layer" }, { k: "check", h: "Check" }, { k: "subject", h: "Subject" }, { k: "status", h: "Status", r: (x) => <Pill value={x.status} /> }, { k: "explanation", h: "Explanation" }]} rows={r.reconciliation.failures.slice(0, 50)} /></Card>}
    </div>
  );
}

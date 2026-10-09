import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Pill, Stat, Table } from "../components/ui";
import { fmtNum } from "../api";

export default function Dashboard() {
  const { projectId, project, source } = useProjectDetails();
  const caps = useApi<any[]>("/platform/capabilities");
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const disc = useApi<any>(source ? `/systems/${source.id}/discovery` : null, undefined, [source?.id]);
  const approved = (manifests.data || []).find((m) => m.status === "APPROVED") || (manifests.data || [])[0];
  const lastRun = (runs.data || [])[0];
  const s = disc.data?.summary;
  return (
    <div>
      <Banner kind="warn">This build executes <b>simulated</b> migrations on synthetic SAP-like data. No SAP system is connected. Capability status is shown per area below.</Banner>
      <ErrorBox error={manifests.error || runs.error} />
      <div className="stats">
        <Stat label="Project" value={project?.name || "-"} sub={project?.scenario_type} />
        <Stat label="Company codes discovered" value={s?.org_units?.COMPANY_CODE ?? "-"} sub={`${s?.org_units?.PLANT ?? "-"} plants`} />
        <Stat label="Objects in scope" value={fmtNum(approved?.impact?.objects_total)} sub={approved ? `${approved.name} v${approved.version}` : "no manifest"} />
        <Stat label="Pending approvals" value={fmtNum(approved?.impact?.approvals_required)} sub={approved?.status && <Pill value={approved.status} />} />
        <Stat label="Last run" value={lastRun ? <Pill value={lastRun.status} /> : "-"} sub={lastRun ? <>Reconciliation <Pill value={lastRun.reconciliation} /></> : ""} />
        <Stat label="Transformation complexity" value={s?.complexity?.band || "-"} sub={s ? `score ${s.complexity.score}` : ""} />
      </div>
      <div className="grid2">
        <Card title="Scope classification (latest manifest)">
          {approved ? <Table cols={[{ k: "c", h: "Classification" }, { k: "n", h: "Objects" }]} rows={Object.entries(approved.impact.by_classification || {}).map(([c, n]) => ({ c, n }))} /> : <div className="muted">Create a manifest in the Scope Designer.</div>}
        </Card>
        <Card title="Capability status">
          <Table cols={[{ k: "area", h: "Area" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "note", h: "Note" }]} rows={caps.data || []} />
        </Card>
      </div>
      <Card title="Recent runs"><Table cols={[{ k: "id", h: "Run", r: (r) => r.id.slice(0, 8) }, { k: "mode", h: "Mode", r: (r) => <Pill value={r.mode} /> }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "reconciliation", h: "Reconciliation", r: (r) => <Pill value={r.reconciliation} /> }, { k: "started_by", h: "Started by" }, { k: "started_at", h: "Started" }]} rows={(runs.data || []).slice(0, 8)} /></Card>
    </div>
  );
}

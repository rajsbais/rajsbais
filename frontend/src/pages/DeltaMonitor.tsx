import { useApi } from "../hooks";
import { Banner, Card, Pill, Table } from "../components/ui";

export default function DeltaMonitor() {
  const q = useApi<any>("/platform/delta/status");
  return (
    <div>
      <Banner kind="danger">Delta capture and near-zero-downtime synchronisation are <b>PLANNED</b>. No change-data-capture adapter exists in this build and no downtime figure is claimed. The design (source-specific CDC, ordering, idempotent replay, recovery) is documented in docs/07-ndt-cdc-consistency-recovery.md.</Banner>
      <Card title="Near-zero-downtime stage model">{q.data && <Table cols={[{ k: "stage", h: "Stage" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }]} rows={q.data.stages.map((s: string) => ({ stage: s, status: q.data.implemented_stages.includes(s) ? "IMPLEMENTED (initial load / simulated)" : "PLANNED" }))} />}<p className="muted">{q.data?.note}</p></Card>
    </div>
  );
}

import { useEffect, useState } from "react";
import { api, J } from "../api";
import { Badge, Card, DataTable, StatusBadge, useAction, ErrorNote } from "../components";

export default function Compliance() {
  const [audit, setAudit] = useState<J[]>([]);
  const [ver, setVer] = useState<J>(null);
  const [caps, setCaps] = useState<J[]>([]);
  const act = useAction();
  const load = () => act.run(async () => { setAudit((await api.get("/api/audit?limit=500")).reverse()); setVer(await api.get("/api/audit/verify")); setCaps(await api.get("/api/capabilities")); });
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <>
      <ErrorNote error={act.error} />
      <Card title="Audit trail integrity" actions={<button onClick={load}>Re-verify</button>}>
        {ver && <p>{ver.valid ? <Badge kind="ok">hash chain valid</Badge> : <Badge kind="bad">chain broken at #{ver.broken_at}</Badge>} · {ver.entries} entries
          <span className="muted small"> Tamper-evident only; production needs an immutable (WORM) sink.</span></p>}
        <DataTable rows={audit} cols={[
          { key: "seq", title: "#" }, { key: "ts", title: "Time" }, { key: "actor", title: "Actor" }, { key: "action", title: "Action" },
          { key: "resource", title: "Resource" }, { key: "details", title: "Details", render: (e) => <code className="small">{JSON.stringify(e.details).slice(0, 90)}</code> }]} pageSize={10} />
      </Card>
      <Card title="Capability matrix — what is real and what is not">
        <DataTable rows={caps} cols={[
          { key: "module", title: "Module" }, { key: "feature", title: "Capability" },
          { key: "status", title: "Status", render: (c) => <StatusBadge s={c.status === "implemented-simulated" ? "warn" : c.status === "planned" ? "fail" : "info"} /> },
          { key: "sap_compatibility", title: "SAP compatibility" }, { key: "constraints", title: "Constraints" },
          { key: "test_evidence", title: "Test evidence" }, { key: "remaining_work", title: "Remaining for production" }]} pageSize={8} />
      </Card>
    </>
  );
}

import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Pre, Select, Table, Tabs } from "../components/ui";

export default function Compliance() {
  const { projectId } = useProjectDetails();
  const events = useApi<any[]>("/audit/events", { limit: 300 });
  const verify = useApi<any>("/audit/verify");
  const approvals = useApi<any[]>("/audit/approvals");
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const [rid, setRid] = useState("");
  const id = rid || runs.data?.[0]?.id;
  const ev = useApi<any>(id ? `/runs/${id}/evidence` : null, undefined, [id]);
  const [comp, setComp] = useState<any>(null); const [err, setErr] = useState<string | null>(null);
  const check = async () => { setErr(null); try { setComp(await api(`/projects/${projectId}/agents/compliance/run`, { body: { context: { manifest_id: manifests.data?.[0]?.id, target_country: "DE" } } })); } catch (e: any) { setErr(e.message); } };
  const restricted = events.error?.includes("permission");
  return (
    <div>
      {restricted && <Banner kind="warn">Audit events require the auditor or admin role. Sign in as auditor to view the hash-chained trail.</Banner>}
      <ErrorBox error={err} />
      <Tabs tabs={[
        { id: "ev", label: "Evidence package", content: <Card title={`Run ${id?.slice(0, 8) || ""}`} actions={<Select value={id} onChange={setRid} options={(runs.data || []).map((r) => ({ value: r.id, label: `${r.id.slice(0, 8)} ${r.status}` }))} />}>{ev.data && <><Table cols={[{ k: "name", h: "File" }, { k: "sha256", h: "SHA-256" }, { k: "bytes", h: "Bytes" }]} rows={Object.entries(ev.data.evidence?.files || {}).map(([name, v]: any) => ({ name, ...v }))} /><div className="grid2"><div><h4>Approvals</h4><Table cols={[{ k: "subject_type", h: "Subject" }, { k: "decision", h: "Decision", r: (r) => <Pill value={r.decision} /> }, { k: "kind", h: "Kind" }, { k: "decided_by", h: "By" }]} rows={ev.data.approvals} /></div><div><h4>Audit events of this run</h4><Table cols={[{ k: "ts", h: "At", r: (r) => String(r.ts).slice(0, 19) }, { k: "actor", h: "Actor" }, { k: "action", h: "Action" }, { k: "hash", h: "Hash", r: (r) => r.hash.slice(0, 12) }]} rows={ev.data.audit_events} /></div></div><h4>Execution report</h4><Pre value={ev.data.markdown} /></>}</Card> },
        { id: "audit", label: "Audit trail", content: <Card title="Hash-chained audit events" actions={verify.data && <Pill value={verify.data.ok ? `CHAIN OK (${verify.data.events} events)` : "CHAIN BROKEN"} />}><Table cols={[{ k: "id", h: "#" }, { k: "ts", h: "At", r: (r) => String(r.ts).slice(0, 19) }, { k: "actor", h: "Actor" }, { k: "action", h: "Action" }, { k: "subject_type", h: "Subject" }, { k: "subject_id", h: "Id", r: (r) => r.subject_id.slice(0, 8) }, { k: "details", h: "Details", r: (r) => JSON.stringify(r.details).slice(0, 120) }, { k: "hash", h: "Hash", r: (r) => r.hash.slice(0, 12) }]} rows={events.data || []} empty={restricted ? "restricted" : "No events"} /></Card> },
        { id: "appr", label: "Approvals (four-eyes)", content: <Card><Table cols={[{ k: "subject_type", h: "Subject" }, { k: "subject_id", h: "Id", r: (r) => r.subject_id.slice(0, 8) }, { k: "decision", h: "Decision", r: (r) => <Pill value={r.decision} /> }, { k: "kind", h: "Kind" }, { k: "decided_by", h: "Decided by" }, { k: "comment", h: "Comment" }, { k: "created_at", h: "At", r: (r) => String(r.created_at).slice(0, 19) }]} rows={approvals.data || []} /></Card> },
        { id: "comp", label: "Compliance findings", content: <Card actions={<button onClick={check}>Run compliance agent</button>}>{comp ? <><Table cols={[{ k: "type", h: "Finding" }, { k: "severity", h: "Severity", r: (r) => <Pill value={r.severity} /> }, { k: "count", h: "Count" }, { k: "requirement", h: "Requirement" }]} rows={comp.proposal.findings} /><KV obj={{ confidence: comp.confidence, status: comp.status }} /></> : <p className="muted">Checks export control, cross-border transfer, TSA retention and personal data exposure for the latest manifest.</p>}</Card> },
      ]} />
    </div>
  );
}

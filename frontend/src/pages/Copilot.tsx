import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Pill, Pre, Select, Table } from "../components/ui";

export default function Copilot() {
  const { projectId } = useProjectDetails();
  const agents = useApi<any[]>("/agents");
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const decisions = useApi<any[]>(projectId ? `/projects/${projectId}/agents/decisions` : null);
  const [agent, setAgent] = useState("scope_recommendation"); const [mid, setMid] = useState(""); const [rid, setRid] = useState(""); const [cc, setCc] = useState("5000");
  const [res, setRes] = useState<any>(null); const [err, setErr] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const run = async () => { setBusy(true); setErr(null); try { setRes(await api(`/projects/${projectId}/agents/${agent}/run`, { body: { context: { manifest_id: mid || manifests.data?.[0]?.id, run_id: rid || runs.data?.[0]?.id, company_code: cc, downtime_window_hours: 24 } } })); decisions.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const decide = async (id: string, accept: boolean) => { setErr(null); try { await api(`/agents/decisions/${id}/decide`, { body: { accept } }); decisions.reload(); } catch (e: any) { setErr(e.message); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  return (
    <div>
      <Banner>Agents operate on stored metadata and evidence only. They propose; a human with approval rights decides. Agents cannot authorise production migration, delete data, post financial adjustments or change security policy. Reasoning is deterministic (heuristic reasoner); an LLM-backed reasoner is planned behind the same interface.</Banner>
      <Card title="Run an agent" actions={<><Select value={agent} onChange={setAgent} options={(agents.data || []).map((a) => ({ value: a.name, label: a.name }))} /><Select value={mid} onChange={setMid} options={(manifests.data || []).map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} placeholder="manifest (default latest)" /><Select value={rid} onChange={setRid} options={(runs.data || []).map((r) => ({ value: r.id, label: r.id.slice(0, 8) }))} placeholder="run (default latest)" /><input value={cc} onChange={(e) => setCc(e.target.value)} style={{ width: 80 }} /><button disabled={busy} onClick={run}>{busy ? "Thinking…" : "Propose"}</button></>}>
        <p className="muted">{(agents.data || []).find((a) => a.name === agent)?.description}</p>
        <ErrorBox error={err} />
        {res && <><p><b>{res.proposal.summary}</b> · confidence {res.confidence} · <Pill value={res.status} /> · evidence: {res.evidence.map((e: any) => `${e.kind}:${e.ref.slice(0, 8)}`).join(", ")}</p>{res.proposal.markdown ? <Pre value={res.proposal.markdown} /> : res.proposal.ruleset_yaml ? <Pre value={res.proposal.ruleset_yaml} /> : <Pre value={Object.fromEntries(Object.entries(res.proposal).filter(([k]) => !["summary", "forbidden_actions", "requires_approval"].includes(k)))} />}</>}
      </Card>
      <div className="grid2">
        <Card title="Agent catalog"><Table cols={[{ k: "name", h: "Agent" }, { k: "description", h: "Purpose" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }]} rows={agents.data || []} /></Card>
        <Card title="Decision log"><Table cols={[{ k: "agent", h: "Agent" }, { k: "s", h: "Summary", r: (r) => r.proposal.summary }, { k: "confidence", h: "Conf." }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "decided_by", h: "Decided by" }, { k: "x", h: "", r: (r) => r.status === "PROPOSED" && r.proposal.requires_approval ? <><button className="secondary" onClick={() => decide(r.id, true)}>Accept</button> <button className="danger" onClick={() => decide(r.id, false)}>Reject</button></> : null }]} rows={decisions.data || []} /></Card>
      </div>
    </div>
  );
}

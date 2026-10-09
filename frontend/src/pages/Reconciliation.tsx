import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Pill, Select, Stat, Table } from "../components/ui";

export default function Reconciliation() {
  const { projectId } = useProjectDetails();
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const [sel, setSel] = useState(""); const [layer, setLayer] = useState(""); const [status, setStatus] = useState("");
  const id = sel || runs.data?.[0]?.id;
  const run = (runs.data || []).find((r) => r.id === id);
  const rep = useApi<any>(id ? `/runs/${id}/report` : null, undefined, [id]);
  const rows = useApi<any[]>(id ? `/runs/${id}/reconciliation` : null, { layer, status, limit: 1000 }, [id, layer, status]);
  const appr = useApi<any[]>(id ? "/audit/approvals" : null, { subject_id: id }, [id]);
  const [err, setErr] = useState<string | null>(null); const [comment, setComment] = useState("");
  const [expl, setExpl] = useState<any>(null);
  const signoff = async (kind: string, decision: string) => { setErr(null); try { await api(`/runs/${id}/signoff`, { body: { kind, decision, comment } }); appr.reload(); } catch (e: any) { setErr(e.message); } };
  const explain = async () => { setErr(null); try { setExpl(await api(`/projects/${projectId}/agents/reconciliation_explanation/run`, { body: { context: { run_id: id } } })); } catch (e: any) { setErr(e.message); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  if (!runs.data?.length) return <Banner>No runs yet.</Banner>;
  const by = rep.data?.reconciliation?.by_layer || {};
  return (
    <div>
      <div className="row"><Select value={id} onChange={setSel} options={(runs.data || []).map((r) => ({ value: r.id, label: `${r.id.slice(0, 8)} ${r.status} ${r.started_at?.slice(0, 16)}` }))} /><Select value={layer} onChange={setLayer} options={["TECHNICAL", "FUNCTIONAL", "FINANCIAL"].map((l) => ({ value: l, label: l }))} placeholder="all layers" /><Select value={status} onChange={setStatus} options={["PASS", "WARN", "FAIL"].map((l) => ({ value: l, label: l }))} placeholder="all statuses" /><button className="secondary" onClick={explain}>Explain variances (agent)</button></div>
      <ErrorBox error={err || rows.error} />
      <div className="stats"><Stat label="Overall" value={<Pill value={rep.data?.reconciliation?.overall} />} sub={`${rep.data?.reconciliation?.checks ?? 0} checks`} />{["TECHNICAL", "FUNCTIONAL", "FINANCIAL"].map((l) => <Stat key={l} label={l} value={`${by[l]?.PASS || 0} pass`} sub={`${by[l]?.WARN || 0} warn · ${by[l]?.FAIL || 0} fail`} />)}<Stat label="Technical load ≠ financial sign-off" value={run?.status === "COMPLETED" ? "load OK" : run?.status} sub="financial & functional layers are independent" /></div>
      <Card title="Checks" actions={<><input placeholder="sign-off comment" value={comment} onChange={(e) => setComment(e.target.value)} /><button onClick={() => signoff("TECHNICAL", "APPROVED")}>Technical sign-off</button> <button onClick={() => signoff("BUSINESS", "APPROVED")}>Business sign-off</button> <button className="danger" onClick={() => signoff("BUSINESS", "REJECTED")}>Reject</button></>}>
        <Table cols={[{ k: "layer", h: "Layer" }, { k: "check", h: "Check" }, { k: "subject", h: "Subject" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "source", h: "Source" }, { k: "target", h: "Target" }, { k: "variance", h: "Variance" }, { k: "explanation", h: "Explanation / evidence" }]} rows={rows.data || []} />
      </Card>
      <div className="grid2">
        <Card title="Sign-offs"><Table cols={[{ k: "kind", h: "Kind" }, { k: "decision", h: "Decision", r: (r) => <Pill value={r.decision} /> }, { k: "decided_by", h: "By" }, { k: "comment", h: "Comment" }, { k: "created_at", h: "At" }]} rows={appr.data || []} empty="No sign-offs recorded" /></Card>
        <Card title="Variance explanations">{expl ? <Table cols={[{ k: "check", h: "Check" }, { k: "subject", h: "Subject" }, { k: "root_cause_category", h: "Root cause", r: (r) => <Pill value={r.root_cause_category} /> }, { k: "explanation", h: "Explanation" }, { k: "recommended_action", h: "Action" }]} rows={expl.proposal.explanations} empty="All checks passed - nothing to explain" /> : <div className="muted">Run the explanation agent.</div>}</Card>
      </div>
    </div>
  );
}

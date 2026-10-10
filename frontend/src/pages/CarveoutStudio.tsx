import { useEffect, useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Facts, Pill, Select, Stat, Table, Tabs } from "../components/ui";
import { sharedRisk } from "../lib";

const POLICIES = [{ value: "REFERENCE", label: "Reference only" }, { value: "DUPLICATE", label: "Duplicate into the target" }, { value: "EXCLUDE", label: "Exclude" }, { value: "MANUAL", label: "Manual disposition (business decides)" }];

export default function CarveoutStudio() {
  const { projectId, source, target } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const org = useApi<any>(source ? `/systems/${source.id}/org-structure` : null, undefined, [source?.id]);
  const [mid, setMid] = useState<string>("");
  const id = mid || manifests.data?.[0]?.id;
  const ccs: any[] = (org.data?.units || []).filter((u: any) => u.type === "COMPANY_CODE");
  const [cc, setCc] = useState("");
  const [policy, setPolicy] = useState("REFERENCE");
  const [genBusy, setGenBusy] = useState(false);
  const [genErr, setGenErr] = useState<string | null>(null);
  useEffect(() => { if (!cc && ccs.length) setCc(ccs.find((u) => u.code !== ccs[0].code)?.code || ccs[0].code); }, [ccs.length]);
  const generate = async () => {
    if (!cc || !source || !target) return;
    setGenBusy(true); setGenErr(null);
    try {
      const plants = (org.data?.units || []).filter((u: any) => u.type === "PLANT" && u.parent_code === cc).map((u: any) => u.code);
      const kokrs = ccs.find((u) => u.code === cc)?.attributes?.KOKRS;
      const created = await api(`/projects/${projectId}/manifests`, { body: { name: `Carve-out ${cc}`, description: `Generated in the Carve-out studio: company code ${cc}, shared objects ${policy.toLowerCase()}`, scenario_type: "CARVE_OUT", carve_out_direction: "FORWARD", source_system_id: source.id, target_system_id: target.id, company_codes: [cc], plants: [], shared_object_policy: policy, cross_company_policy: "INCLUDE_FLAG", historical_policy: "FULL", document_status: "ALL", target_ownership: { company_code_map: { [cc]: cc }, plant_map: Object.fromEntries(plants.map((x: string) => [x, x])), controlling_area_map: kokrs ? { [kokrs]: kokrs } : {} } } });
      setMid(created.id); manifests.reload();
    } catch (e: any) { setGenErr(e.message); } finally { setGenBusy(false); }
  };
  const m = (manifests.data || []).find((x) => x.id === id);
  const cls = useApi<any>(id ? `/manifests/${id}/carveout/classification` : null, undefined, [id]);
  const comp = useApi<any>(id ? `/manifests/${id}/carveout/completeness` : null, undefined, [id]);
  const res = useApi<any>(id ? `/manifests/${id}/carveout/residual` : null, undefined, [id]);
  const pending = useApi<any>(id ? `/manifests/${id}/objects` : null, { requires_approval: true, limit: 500 }, [id]);
  const [bucket, setBucket] = useState("PARTIALLY_TRANSFERRED");
  const [err, setErr] = useState<string | null>(null);
  const [comment, setComment] = useState("Business reviewed");
  const reloadAll = () => { manifests.reload(); cls.reload(); comp.reload(); res.reload(); pending.reload(); };
  const disposition = async (decision: string, nodes?: string[]) => { setErr(null); try { await api(`/manifests/${id}/dispositions`, { body: { all_pending: !nodes, nodes: nodes || [], decision, comment } }); reloadAll(); } catch (e: any) { setErr(e.message); } };
  const approve = async (ok: boolean) => { setErr(null); try { await api(`/manifests/${id}/${ok ? "approve" : "reject"}`, { body: { comment } }); reloadAll(); } catch (e: any) { setErr(e.message); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  const counts = cls.data?.counts || {};
  const risk = sharedRisk(m?.impact?.by_classification);
  const generator = (
    <Card>
      <label className="field">Company code<select value={cc} onChange={(e) => setCc(e.target.value)} disabled={!ccs.length}>{ccs.map((u) => <option key={u.code} value={u.code}>{u.code} — {u.name}</option>)}{!ccs.length && <option value="">run discovery first (Landscape)</option>}</select></label>
      <label className="field">Shared object policy<select value={policy} onChange={(e) => setPolicy(e.target.value)}>{POLICIES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
      <button className="block" disabled={genBusy || !cc || !source || !target} onClick={generate}>{genBusy ? "Evaluating the scope…" : "Generate scope and analyze"}</button>
      <ErrorBox error={genErr} />
      <p className="muted" style={{ marginTop: 12 }}>Seeds the scope with the company code, traverses the dependency graph with the shared-object policy, classifies every object and writes a versioned, hashed manifest (DRAFT until approved). The full scope definition (plants, fiscal years, historical policy, target ownership maps, what-if compare) is in the Scope designer.</p>
    </Card>
  );
  if (!manifests.data?.length) return <div>{generator}<Card><p className="muted" style={{ margin: 0 }}>Generate a scope to see the immutable manifest.</p></Card></div>;
  return (
    <div>
      {generator}
      {m && <Card title="Immutable manifest" actions={<Pill value={m.status} />}>
        <Facts rows={[{ k: "Manifest", v: `${m.name} v${m.version}` }, { k: "Manifest hash", v: m.content_hash.slice(0, 16), mono: true }, { k: "Related objects", v: fmtNum(m.impact?.objects_total) }, { k: "Shared exposure", v: <Pill value={risk.band} /> }, { k: "Approvals required", v: fmtNum(m.impact?.approvals_required) }, { k: "Created by", v: `${m.created_by}${m.approved_by ? ` · approved by ${m.approved_by}` : ""}` }]} />
      </Card>}
      <div className="row"><Select value={id} onChange={setMid} options={(manifests.data || []).map((x) => ({ value: x.id, label: `${x.name} v${x.version} (${x.status})` }))} />{m && <Pill value={m.status} />}<input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="approval comment" style={{ minWidth: 260 }} /><button className="secondary" onClick={() => disposition("TRANSFER")}>Disposition all pending: TRANSFER</button><button className="secondary" onClick={() => disposition("RETAIN")}>all pending: RETAIN</button><button disabled={m?.status !== "DRAFT"} onClick={() => approve(true)}>Approve manifest (business)</button><button className="danger" disabled={m?.status !== "DRAFT"} onClick={() => approve(false)}>Reject</button></div>
      <ErrorBox error={err || cls.error} />
      <div className="stats">{["FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED", "RETAINED_BY_SELLER", "REFERENCE_ONLY", "EXCLUDED", "MANUAL_DISPOSITION"].map((k) => <Stat key={k} label={k.replace(/_/g, " ").toLowerCase()} value={fmtNum(counts[k] || 0)} />)}</div>
      <Tabs tabs={[
        { id: "pend", label: `Pending business dispositions (${pending.data?.pending_dispositions ?? 0})`, content: <Card><p className="muted">Ambiguous ownership and legal disposition require explicit business approval before the manifest can be approved. Approvers decide per object or in bulk.</p><Table cols={[{ k: "node", h: "Object" }, { k: "classification", h: "Current", r: (r) => <Pill value={r.classification} /> }, { k: "company_codes", h: "Company codes", r: (r) => r.company_codes.join(", ") }, { k: "reason", h: "Reason" }, { k: "x", h: "", r: (r) => <>{["TRANSFER", "RETAIN", "DUPLICATE", "REFERENCE", "EXCLUDE"].map((d) => <button key={d} className="secondary" style={{ marginRight: 4 }} onClick={() => disposition(d, [r.node])}>{d}</button>)}</> }]} rows={pending.data?.items} empty="No pending dispositions" /></Card> },
        { id: "det", label: "Detections", content: <div className="grid2"><Card title="Cross-company and shared-data detections"><Table cols={[{ k: "title", h: "Detection" }, { k: "object_type", h: "Object type" }, { k: "count", h: "Count" }, { k: "samples", h: "Samples", r: (r) => r.samples.slice(0, 3).join(", ") }]} rows={comp.data?.detections} /></Card><Card title="Intercompany balances (open, in-scope company codes)"><Table cols={[{ k: "company_code", h: "CC" }, { k: "counterpart", h: "Counterpart" }, { k: "counterpart_in_scope", h: "In scope", r: (r) => (r.counterpart_in_scope ? "yes" : "no") }, { k: "receivable", h: "Receivable", r: (r) => fmtNum(r.receivable) }, { k: "payable", h: "Payable", r: (r) => fmtNum(r.payable) }, { k: "currency", h: "Cur." }]} rows={comp.data?.intercompany_balances} /></Card></div> },
        { id: "cls", label: "Classification buckets", content: <Card actions={<Select value={bucket} onChange={setBucket} options={Object.keys(counts).map((k) => ({ value: k, label: `${k} (${counts[k]})` }))} />}><Table cols={[{ k: "node", h: "Object" }, { k: "type", h: "Type" }, { k: "company_codes", h: "Company codes", r: (r) => r.company_codes.join(", ") }, { k: "status", h: "Status" }, { k: "reason", h: "Reason" }]} rows={cls.data?.buckets?.[bucket]} /></Card> },
        { id: "comp", label: `Completeness ${comp.data?.complete ? "✓" : ""}`, content: <Card title={comp.data ? `Completeness: ${comp.data.complete ? "COMPLETE" : "INCOMPLETE"} (${comp.data.pending_approvals.count} pending approvals)` : ""}><Table cols={[{ k: "name", h: "Object type" }, { k: "owned", h: "Owned by SpinCo" }, { k: "transferred", h: "Transferred" }, { k: "coverage_pct", h: "Coverage %" }]} rows={comp.data?.coverage} /></Card> },
        { id: "res", label: "Residual exposure", content: <div className="grid2"><Card title={`Residual cleanup candidates (${res.data?.residual_cleanup_candidates?.count ?? 0})`}><p className="muted">{res.data?.residual_cleanup_candidates?.note}</p><Table cols={[{ k: "table", h: "Table" }, { k: "key", h: "Key" }, { k: "object", h: "Object" }, { k: "action", h: "Action", r: (r) => <Pill value={r.action} /> }]} rows={res.data?.residual_cleanup_candidates?.samples} /></Card><Card title="Retained SpinCo history, TSA services, export control"><p>Retained history objects: {res.data?.retained_spinco_history?.count} · Export-control flags: {res.data?.export_control_flags} · Excluded by policy: {JSON.stringify(res.data?.excluded_by_policy)}</p><Table cols={[{ k: "BUKRS", h: "CC" }, { k: "TSA_ID", h: "TSA" }, { k: "SERVICE", h: "Service" }, { k: "END_DATE", h: "End" }]} rows={res.data?.tsa_services} /></Card></div> },
      ]} />
    </div>
  );
}

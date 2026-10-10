import { useEffect, useState } from "react";
import { api, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Facts, Pill, Select, Stat, Table, Tabs } from "../components/ui";
import { sharedRisk } from "../lib";

const DEALS = [{ value: "", label: "No deal template (policies by hand)" }, { value: "ASSET_DEAL", label: "Asset deal: entity stays, open items and balances move" }, { value: "SHARE_DEAL", label: "Share deal: entity and full history move" }, { value: "HIVE_DOWN", label: "Hive-down: new entity in the target, then sold" }];
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
  const [deal, setDeal] = useState("");
  const [newCc, setNewCc] = useState("");
  const [genBusy, setGenBusy] = useState(false);
  const [genErr, setGenErr] = useState<string | null>(null);
  useEffect(() => { if (!cc && ccs.length) setCc(ccs.find((u) => u.code !== ccs[0].code)?.code || ccs[0].code); }, [ccs.length]);
  const generate = async () => {
    if (!cc || !source || !target) return;
    setGenBusy(true); setGenErr(null);
    try {
      const plants = (org.data?.units || []).filter((u: any) => u.type === "PLANT" && u.parent_code === cc).map((u: any) => u.code);
      const kokrs = ccs.find((u) => u.code === cc)?.attributes?.KOKRS;
      const ownership = { company_code_map: { [cc]: cc }, plant_map: Object.fromEntries(plants.map((x: string) => [x, x])), controlling_area_map: kokrs ? { [kokrs]: kokrs } : {} };
      const created = deal
        ? await api(`/projects/${projectId}/manifests/from-deal`, { body: { deal, new_company_code: deal === "HIVE_DOWN" ? newCc : null, definition: { name: `Carve-out ${cc} (${deal.toLowerCase().replace("_", " ")})`, description: `Generated in the Carve-out studio: company code ${cc}, shared objects ${policy.toLowerCase()}`, scenario_type: "CARVE_OUT", carve_out_direction: "FORWARD", source_system_id: source.id, target_system_id: target.id, company_codes: [cc], plants: [], shared_object_policy: policy, target_ownership: ownership } } })
        : await api(`/projects/${projectId}/manifests`, { body: { name: `Carve-out ${cc}`, description: `Generated in the Carve-out studio: company code ${cc}, shared objects ${policy.toLowerCase()}`, scenario_type: "CARVE_OUT", carve_out_direction: "FORWARD", source_system_id: source.id, target_system_id: target.id, company_codes: [cc], plants: [], shared_object_policy: policy, cross_company_policy: "INCLUDE_FLAG", historical_policy: "FULL", document_status: "ALL", target_ownership: ownership } });
      setMid(created.id); manifests.reload();
    } catch (e: any) { setGenErr(e.message); } finally { setGenBusy(false); }
  };
  const m = (manifests.data || []).find((x) => x.id === id);
  const cls = useApi<any>(id ? `/manifests/${id}/carveout/classification` : null, undefined, [id]);
  const comp = useApi<any>(id ? `/manifests/${id}/carveout/completeness` : null, undefined, [id]);
  const res = useApi<any>(id ? `/manifests/${id}/carveout/residual` : null, undefined, [id]);
  const pending = useApi<any>(id ? `/manifests/${id}/objects` : null, { requires_approval: true, limit: 500 }, [id]);
  const dealQ = useApi<any>(id ? `/manifests/${id}/carveout/deal` : null, undefined, [id]);
  const plans = useApi<any[]>(id ? `/manifests/${id}/carveout/cleanup-plans` : null, undefined, [id]);
  const [planId, setPlanId] = useState("");
  const [plan, setPlan] = useState<any>(null);
  const [planErr, setPlanErr] = useState<string | null>(null);
  useEffect(() => { setPlanId(""); setPlan(null); }, [id]);
  useEffect(() => { if (!planId && plans.data?.length) setPlanId(plans.data[plans.data.length - 1].id); }, [plans.data, planId]);
  useEffect(() => { if (!planId) { setPlan(null); return; } api(`/carveout/cleanup-plans/${planId}`).then(setPlan).catch((e: any) => setPlanErr(e.message)); }, [planId]);
  const planCall = async (fn: () => Promise<any>, after?: (x: any) => void) => { setPlanErr(null); try { const x = await fn(); if (after) after(x); else if (planId) setPlan(await api(`/carveout/cleanup-plans/${planId}`)); plans.reload(); res.reload(); } catch (e: any) { setPlanErr(e.message); } };
  const createPlan = () => planCall(() => api(`/manifests/${id}/carveout/cleanup-plans`, { method: "POST", body: {} }), (x) => { setPlanId(x.id); setPlan(x); plans.reload(); });
  const excludeItem = (it: any) => { const note = window.prompt(`Exclude ${it.id} (${it.table} ${it.key}): why?`, ""); if (!note) return; planCall(() => api(`/carveout/cleanup-plans/${planId}/items/${it.id}`, { body: { decision: "EXCLUDE", note } })); };
  const includeItem = (it: any) => planCall(() => api(`/carveout/cleanup-plans/${planId}/items/${it.id}`, { body: { decision: "INCLUDE", note: "" } }));
  const approvePlan = (ok: boolean) => { const c = window.prompt(ok ? "Approve the cleanup plan (business, four eyes): comment" : "Reject the plan: why?", ""); if (c === null) return; planCall(() => api(`/carveout/cleanup-plans/${planId}/${ok ? "approve" : "reject"}`, { body: { comment: c } })); };
  const exportPlan = () => planCall(() => api(`/carveout/cleanup-plans/${planId}/export`, { method: "POST", body: {} }));
  const executePlan = () => { if (!window.confirm("Execute the approved plan on the platform's simulated source? The package is written first; the included rows are then removed from the record store. No SAP system is changed.")) return; planCall(() => api(`/carveout/cleanup-plans/${planId}/execute`, { method: "POST", body: {} })); };
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
      <label className="field">Deal template<select value={deal} onChange={(e) => setDeal(e.target.value)}>{DEALS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
      {deal === "HIVE_DOWN" && <label className="field">New company code in the target<input value={newCc} onChange={(e) => setNewCc(e.target.value.toUpperCase().slice(0, 4))} placeholder="NC01" /></label>}
      <button className="block" disabled={genBusy || !cc || !source || !target || (deal === "HIVE_DOWN" && !newCc)} onClick={generate}>{genBusy ? "Evaluating the scope…" : "Generate scope and analyze"}</button>
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
      {dealQ.data && <Card title={dealQ.data.deal_type ? `Deal: ${dealQ.data.template.name}` : "Deal template"} actions={dealQ.data.deal_type && <Pill value={dealQ.data.residual_rule} />}>
        {dealQ.data.deal_type ? <>
          <p className="muted">{dealQ.data.template.summary} {dealQ.data.residual_note}</p>
          <div className="grid2">
            <div><h4>Obligations</h4><ul>{dealQ.data.obligations.map((o: string) => <li key={o}>{o}</li>)}</ul><p className="muted">Approvals: {dealQ.data.approvals.join(", ")}</p></div>
            <div><h4>Deviations from the template ({dealQ.data.deviations.length})</h4>{dealQ.data.deviations.length ? <Table cols={[{ k: "field", h: "Policy" }, { k: "template", h: "Template", r: (r) => String(r.template) }, { k: "actual", h: "Chosen", r: (r) => String(r.actual) }, { k: "consequence", h: "Consequence" }]} rows={dealQ.data.deviations} /> : <p className="muted">The policies follow the template.</p>}</div>
          </div>
        </> : <p className="muted">{dealQ.data.note}</p>}
      </Card>}
      <div className="row"><Select value={id} onChange={setMid} options={(manifests.data || []).map((x) => ({ value: x.id, label: `${x.name} v${x.version} (${x.status})` }))} />{m && <Pill value={m.status} />}<input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="approval comment" style={{ minWidth: 260 }} /><button className="secondary" onClick={() => disposition("TRANSFER")}>Disposition all pending: TRANSFER</button><button className="secondary" onClick={() => disposition("RETAIN")}>all pending: RETAIN</button><button disabled={m?.status !== "DRAFT"} onClick={() => approve(true)}>Approve manifest (business)</button><button className="danger" disabled={m?.status !== "DRAFT"} onClick={() => approve(false)}>Reject</button></div>
      <ErrorBox error={err || cls.error} />
      <div className="stats">{["FULLY_TRANSFERRED", "PARTIALLY_TRANSFERRED", "SHARED_DUPLICATED", "RETAINED_BY_SELLER", "REFERENCE_ONLY", "EXCLUDED", "MANUAL_DISPOSITION"].map((k) => <Stat key={k} label={k.replace(/_/g, " ").toLowerCase()} value={fmtNum(counts[k] || 0)} />)}</div>
      <Tabs tabs={[
        { id: "pend", label: `Pending business dispositions (${pending.data?.pending_dispositions ?? 0})`, content: <Card><p className="muted">Ambiguous ownership and legal disposition require explicit business approval before the manifest can be approved. Approvers decide per object or in bulk.</p><Table cols={[{ k: "node", h: "Object" }, { k: "classification", h: "Current", r: (r) => <Pill value={r.classification} /> }, { k: "company_codes", h: "Company codes", r: (r) => r.company_codes.join(", ") }, { k: "reason", h: "Reason" }, { k: "x", h: "", r: (r) => <>{["TRANSFER", "RETAIN", "DUPLICATE", "REFERENCE", "EXCLUDE"].map((d) => <button key={d} className="secondary" style={{ marginRight: 4 }} onClick={() => disposition(d, [r.node])}>{d}</button>)}</> }]} rows={pending.data?.items} empty="No pending dispositions" /></Card> },
        { id: "det", label: "Detections", content: <div className="grid2"><Card title="Cross-company and shared-data detections"><Table cols={[{ k: "title", h: "Detection" }, { k: "object_type", h: "Object type" }, { k: "count", h: "Count" }, { k: "samples", h: "Samples", r: (r) => r.samples.slice(0, 3).join(", ") }]} rows={comp.data?.detections} /></Card><Card title="Intercompany balances (open, in-scope company codes)"><Table cols={[{ k: "company_code", h: "CC" }, { k: "counterpart", h: "Counterpart" }, { k: "counterpart_in_scope", h: "In scope", r: (r) => (r.counterpart_in_scope ? "yes" : "no") }, { k: "receivable", h: "Receivable", r: (r) => fmtNum(r.receivable) }, { k: "payable", h: "Payable", r: (r) => fmtNum(r.payable) }, { k: "currency", h: "Cur." }]} rows={comp.data?.intercompany_balances} /></Card></div> },
        { id: "cls", label: "Classification buckets", content: <Card actions={<Select value={bucket} onChange={setBucket} options={Object.keys(counts).map((k) => ({ value: k, label: `${k} (${counts[k]})` }))} />}><Table cols={[{ k: "node", h: "Object" }, { k: "type", h: "Type" }, { k: "company_codes", h: "Company codes", r: (r) => r.company_codes.join(", ") }, { k: "status", h: "Status" }, { k: "reason", h: "Reason" }]} rows={cls.data?.buckets?.[bucket]} /></Card> },
        { id: "comp", label: `Completeness ${comp.data?.complete ? "✓" : ""}`, content: <Card title={comp.data ? `Completeness: ${comp.data.complete ? "COMPLETE" : "INCOMPLETE"} (${comp.data.pending_approvals.count} pending approvals)` : ""}><Table cols={[{ k: "name", h: "Object type" }, { k: "owned", h: "Owned by SpinCo" }, { k: "transferred", h: "Transferred" }, { k: "coverage_pct", h: "Coverage %" }]} rows={comp.data?.coverage} /></Card> },
        { id: "res", label: "Residual exposure", content: <>
          <div className="grid2"><Card title={`Residual cleanup candidates (${res.data?.residual_cleanup_candidates?.count ?? 0})`}><p className="muted">{res.data?.residual_cleanup_candidates?.note}{res.data?.deal?.residual_rule ? ` Deal rule ${res.data.deal.residual_rule}: ${res.data.deal.residual_note}` : ""}</p><Table cols={[{ k: "table", h: "Table" }, { k: "key", h: "Key" }, { k: "object", h: "Object" }, { k: "action", h: "Action", r: (r) => <Pill value={r.action} /> }]} rows={res.data?.residual_cleanup_candidates?.samples} /></Card><Card title="Retained SpinCo history, TSA services, export control"><p>Retained history objects: {res.data?.retained_spinco_history?.count} · Export-control flags: {res.data?.export_control_flags} · Excluded by policy: {JSON.stringify(res.data?.excluded_by_policy)}</p><Table cols={[{ k: "BUKRS", h: "CC" }, { k: "TSA_ID", h: "TSA" }, { k: "SERVICE", h: "Service" }, { k: "END_DATE", h: "End" }]} rows={res.data?.tsa_services} /></Card></div>
          <Card title="Residual cleanup plans" actions={<><Select value={planId} onChange={setPlanId} options={(plans.data || []).map((x) => ({ value: x.id, label: `Plan ${x.sequence} · ${x.status}` }))} placeholder="plan" /><button onClick={createPlan}>Create cleanup plan</button></>}>
            <p className="muted">A plan takes every candidate as an item; exclude items with a note while it is a draft. Approval is a business decision under four eyes and is refused until the manifest is approved and a completed run has reconciled. The work package (CSV per table, JSON index, zip) is the hand-over for the SAP-side archiving run; execution happens on the platform's simulated source only, after the package is written. No SAP system is ever changed from here.</p>
            <ErrorBox error={planErr} />
            {plan && <>
              <div className="stats"><Stat label="Plan" value={`${plan.sequence}`} sub={<Pill value={plan.status} />} /><Stat label="Included / excluded" value={`${plan.summary.included} / ${plan.summary.excluded}`} sub={Object.entries(plan.summary.by_action || {}).map(([a, n]) => `${a}: ${n}`).join(" · ")} /><Stat label="Would change the source" value={plan.summary.deletes} sub={plan.residual_rule || "as reported"} tone={plan.summary.deletes ? "warn" : "good"} /><Stat label="Approved / executed" value={`${plan.approved_by || "–"} / ${plan.executed_by || "–"}`} sub={plan.execution?.note || plan.approval_comment || ""} /></div>
              <div className="row">
                {plan.status === "DRAFT" && <><button onClick={() => approvePlan(true)}>Approve (business, four eyes)</button><button className="danger" onClick={() => approvePlan(false)}>Reject</button></>}
                <button className="secondary" onClick={exportPlan}>Export work package</button>
                {plan.status === "APPROVED" && <button onClick={executePlan}>Execute on the simulated source</button>}
              </div>
              {plan.package?.zip && <p className="muted">Package: {plan.package.zip} · {Object.keys(plan.package.files || {}).length} files · exported {String(plan.package.exported_at).slice(0, 16)}{plan.package.missing_in_source?.length ? ` · ${plan.package.missing_in_source.length} item(s) no longer in the source` : ""}</p>}
              {plan.execution?.removed_rows && <p className="muted">Executed: removed rows {JSON.stringify(plan.execution.removed_rows)} · results {JSON.stringify(plan.execution.results)}</p>}
              <Table cols={[{ k: "id", h: "Item" }, { k: "table", h: "Table" }, { k: "key", h: "Key" }, { k: "object", h: "Object" }, { k: "action", h: "Action", r: (it) => <Pill value={it.action} /> }, { k: "decision", h: "Decision", r: (it) => <>{<Pill value={it.decision} />}{it.note && <div className="muted" style={{ fontSize: 12 }}>{it.note}</div>}</> }, { k: "result", h: "Result", r: (it) => it.result ? <Pill value={it.result} /> : "" }, { k: "x", h: "", r: (it) => plan.status === "DRAFT" ? (it.decision === "INCLUDE" ? <button className="secondary" onClick={() => excludeItem(it)}>Exclude</button> : <button className="secondary" onClick={() => includeItem(it)}>Include</button>) : "" }]} rows={plan.items.slice(0, 200)} keyFn={(it) => it.id} />
              {plan.items.length > 200 && <p className="muted">{plan.items.length - 200} more items in the report and the package.</p>}
            </>}
            {!plan && !plans.data?.length && <p className="muted">No cleanup plan yet for this manifest.</p>}
          </Card>
        </> },
      ]} />
    </div>
  );
}

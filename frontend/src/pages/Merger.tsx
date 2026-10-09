import { useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Table } from "../components/ui";

export default function Merger() {
  const { projectId, project } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const dups = useApi<any>(projectId ? `/projects/${projectId}/merge/duplicates` : null);
  const [members, setMembers] = useState<{ manifest_id: string; ruleset_id: string }[]>([{ manifest_id: "", ruleset_id: "" }, { manifest_id: "", ruleset_id: "" }]);
  const [plan, setPlan] = useState<any>(null);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const approvedM = (manifests.data || []).filter((m) => m.status === "APPROVED");
  const approvedR = (rulesets.data || []).filter((r) => r.status === "APPROVED");
  const set = (i: number, k: string, v: string) => setMembers(members.map((m, j) => (j === i ? { ...m, [k]: v } : m)));
  const ready = members.every((m) => m.manifest_id && m.ruleset_id);
  const doPlan = async () => { setBusy(true); setErr(null); try { setPlan(await api(`/projects/${projectId}/merge/plan`, { body: { sources: members } })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const doRun = async () => { setBusy(true); setErr(null); try { setResult(await api(`/projects/${projectId}/merge/run`, { body: { sources: members } })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  const sources = (project?.systems || []).filter((s: any) => s.role === "SOURCE");
  return (
    <div>
      <Banner kind="warn">Multi-source merger (scenario A4): one approved manifest per source system is executed into one shared target. The planner applies each source's rules to every record key in scope and refuses to run while cross-system key collisions exist. Financial reconciliation happens at group level because the merged company code holds the union of all sources. Runtime is <b>simulated</b>.</Banner>
      <Card title={`Merge group (${sources.length} source systems in project)`} actions={<><button className="secondary" onClick={() => setMembers([...members, { manifest_id: "", ruleset_id: "" }])}>+ source</button> <button className="secondary" disabled={!ready || busy} onClick={doPlan}>Plan (collision check)</button> <button disabled={!plan?.ready || busy} onClick={doRun}>{busy ? "Running…" : "Run merge"}</button></>}>
        <Table cols={[{ k: "i", h: "#", r: (r) => r.i + 1 }, { k: "manifest", h: "Approved manifest", r: (r) => <select value={r.m.manifest_id} onChange={(e) => set(r.i, "manifest_id", e.target.value)}><option value="">select…</option>{approvedM.map((m) => <option key={m.id} value={m.id}>{m.name} v{m.version} (CC {m.definition.company_codes.join(",")})</option>)}</select> }, { k: "ruleset", h: "Approved ruleset", r: (r) => <select value={r.m.ruleset_id} onChange={(e) => set(r.i, "ruleset_id", e.target.value)}><option value="">select…</option>{approvedR.map((x) => <option key={x.id} value={x.id}>{x.name} v{x.version}</option>)}</select> }, { k: "hint", h: "Rule factory hint", r: (r) => (r.i === 0 ? "leading source (source_index 0)" : `source_index ${r.i}: disjoint number ranges, BP${r.i + 1} prefix, dedup lookups from /merge/dedup`) }]} rows={members.map((m, i) => ({ i, m }))} />
        <ErrorBox error={err} />
      </Card>
      {plan && <div className="grid2">
        <Card title={`Plan: ${plan.ready ? "READY" : "BLOCKED"}`}><p><Pill value={plan.ready ? "PASS" : "FAIL"} /> {plan.recommendation}</p><KV obj={{ target_company_codes: plan.target_company_codes.join(", "), merged_from_several_sources: plan.company_codes_merged_from_several_sources.join(", ") || "none", collisions: plan.collisions.count }} /><Table cols={[{ k: "sid", h: "Source" }, { k: "company_codes", h: "Company codes", r: (r) => r.company_codes.join(",") }, { k: "target_company_codes", h: "→ Target", r: (r) => r.target_company_codes.join(",") }, { k: "objects", h: "Records planned" }, { k: "skipped_by_rules", h: "Skipped (dedup)" }, { k: "rejected_by_rules", h: "Rejected" }]} rows={plan.sources} /></Card>
        <Card title="Cross-system key collisions"><Table cols={[{ k: "table", h: "Table" }, { k: "target_key", h: "Target key" }, { k: "object_type", h: "Object" }]} rows={plan.collisions.samples} empty="No collisions" />{Object.keys(plan.collisions.by_table).length > 0 && <KV obj={plan.collisions.by_table} />}</Card>
      </div>}
      {result && <Card title={`Merge group ${result.merge_group.slice(0, 8)}: overall ${result.overall}`}><Table cols={[{ k: "id", h: "Run", r: (r) => r.id.slice(0, 8) }, { k: "source_system_id", h: "Source", r: (r) => r.source_system_id.slice(0, 8) }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "reconciliation", h: "Technical + functional", r: (r) => <Pill value={r.reconciliation} /> }]} rows={result.runs} /><p>Group financial reconciliation: <Pill value={result.financial.overall} /> over {result.financial.checks} checks ({result.financial.sources} sources) — details in the Reconciliation Center under the last run of the group.</p></Card>}
      <Card title="Master-data duplicate candidates across source systems">{dups.data?.counts ? <><KV obj={dups.data.counts} /><Table cols={[{ k: "match_key", h: "Match key" }, { k: "survivor", h: "Survivor", r: (r) => `${r.survivor.system.slice(0, 6)}:${r.survivor.key}` }, { k: "duplicates", h: "Duplicates", r: (r) => r.duplicates.map((d: any) => `${d.system.slice(0, 6)}:${d.key}`).join(", ") }, { k: "confidence", h: "Confidence" }]} rows={(dups.data.candidates.customers || []).slice(0, 30)} /></> : <p className="muted">{dups.data?.note || "Register at least two source systems."}</p>}</Card>
    </div>
  );
}

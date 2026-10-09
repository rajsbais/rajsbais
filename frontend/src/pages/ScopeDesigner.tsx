import { useState } from "react";
import { api, fmtBytes, fmtNum } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Stat, Table } from "../components/ui";

const DEFAULT = { name: "SpinCo 5000 forward carve-out", description: "", scenario_type: "CARVE_OUT", carve_out_direction: "FORWARD", company_codes: "5000", plants: "", fiscal_year_from: "", fiscal_year_to: "", document_status: "ALL", historical_policy: "FULL", shared_object_policy: "DUPLICATE", cross_company_policy: "INCLUDE_FLAG", object_types_exclude: "BASIS.RfcDestination,BASIS.IdocPartner,BASIS.BackgroundJob", cc_map: "5000=SP01", plant_map: "5010=SP10,5020=SP20", kokrs_map: "1000=SP01" };

function parseMap(s: string) { const o: Record<string, string> = {}; s.split(",").map((x) => x.trim()).filter(Boolean).forEach((p) => { const [a, b] = p.split("="); if (a && b) o[a.trim()] = b.trim(); }); return o; }
function list(s: string) { return s.split(",").map((x) => x.trim()).filter(Boolean); }

export default function ScopeDesigner() {
  const { projectId, source, target } = useProjectDetails();
  const [f, setF] = useState<any>(DEFAULT);
  const [preview, setPreview] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const [cmp, setCmp] = useState<{ a: string; b: string }>({ a: "", b: "" });
  const [cmpRes, setCmpRes] = useState<any>(null);
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  const defn = () => ({ name: f.name, description: f.description, scenario_type: f.scenario_type, carve_out_direction: f.carve_out_direction, source_system_id: source.id, target_system_id: target.id, company_codes: list(f.company_codes), plants: list(f.plants), fiscal_year_from: f.fiscal_year_from ? Number(f.fiscal_year_from) : null, fiscal_year_to: f.fiscal_year_to ? Number(f.fiscal_year_to) : null, document_status: f.document_status, historical_policy: f.historical_policy, shared_object_policy: f.shared_object_policy, cross_company_policy: f.cross_company_policy, object_types_exclude: list(f.object_types_exclude), target_ownership: { company_code_map: parseMap(f.cc_map), plant_map: parseMap(f.plant_map), controlling_area_map: parseMap(f.kokrs_map) } });
  const run = async (create: boolean) => { setBusy(true); setErr(null); try { if (create) { await api(`/projects/${projectId}/manifests`, { body: defn() }); manifests.reload(); } else setPreview(await api(`/projects/${projectId}/scopes/evaluate`, { body: defn() })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const compare = async () => { if (cmp.a && cmp.b) try { setCmpRes(await api(`/manifests/${cmp.a}/compare/${cmp.b}`)); } catch (e: any) { setErr(e.message); } };
  if (!source || !target) return <Banner>Select a project with source and target systems.</Banner>;
  const sel = (k: string, opts: string[]) => <label>{k}<select value={f[k]} onChange={(e) => set(k, e.target.value)}>{opts.map((o) => <option key={o}>{o}</option>)}</select></label>;
  return (
    <div>
      <Card title="Scope definition" actions={<><button className="secondary" disabled={busy} onClick={() => run(false)}>Preview impact</button> <button disabled={busy} onClick={() => run(true)}>Create versioned manifest</button></>}>
        <div className="form">
          <label>Name<input value={f.name} onChange={(e) => set("name", e.target.value)} /></label>
          {sel("scenario_type", ["CARVE_OUT", "SDT", "MERGER", "BLUEFIELD"])}{sel("carve_out_direction", ["FORWARD", "REVERSE"])}
          <label>Company codes (comma separated)<input value={f.company_codes} onChange={(e) => set("company_codes", e.target.value)} /></label>
          <label>Plants filter (optional)<input value={f.plants} onChange={(e) => set("plants", e.target.value)} /></label>
          <label>Fiscal year from<input value={f.fiscal_year_from} onChange={(e) => set("fiscal_year_from", e.target.value)} /></label>
          <label>Fiscal year to<input value={f.fiscal_year_to} onChange={(e) => set("fiscal_year_to", e.target.value)} /></label>
          {sel("document_status", ["ALL", "OPEN_ONLY", "CLOSED_ONLY"])}{sel("historical_policy", ["FULL", "OPEN_ITEMS_AND_BALANCES", "YEARS"])}{sel("shared_object_policy", ["DUPLICATE", "REFERENCE", "EXCLUDE", "MANUAL"])}{sel("cross_company_policy", ["INCLUDE_FLAG", "REFERENCE", "EXCLUDE"])}
          <label>Exclude object types<input value={f.object_types_exclude} onChange={(e) => set("object_types_exclude", e.target.value)} /></label>
          <label>Company code map (src=tgt)<input value={f.cc_map} onChange={(e) => set("cc_map", e.target.value)} /></label>
          <label>Plant map<input value={f.plant_map} onChange={(e) => set("plant_map", e.target.value)} /></label>
          <label>Controlling area map<input value={f.kokrs_map} onChange={(e) => set("kokrs_map", e.target.value)} /></label>
        </div>
        <ErrorBox error={err} />
      </Card>
      {preview && <>
        <div className="stats"><Stat label="Objects in scope" value={fmtNum(preview.impact.objects_total)} sub={`${preview.impact.objects_seeded} seeded + ${preview.impact.objects_expanded} expanded`} /><Stat label="Est. volume" value={fmtBytes(preview.impact.est_bytes)} /><Stat label="Open documents" value={preview.impact.open_documents} /><Stat label="Approvals required" value={preview.impact.approvals_required} /><Stat label="Filtered / stopped" value={`${preview.impact.objects_skipped_by_filters} / ${preview.impact.objects_stopped}`} /><Stat label="Missing references" value={preview.impact.missing_references} /></div>
        {preview.impact.warnings.map((w: string) => <Banner key={w} kind="warn">{w}</Banner>)}
        <div className="grid2"><Card title="Classification"><Table cols={[{ k: "c", h: "Classification", r: (r) => <Pill value={r.c} /> }, { k: "n", h: "Objects" }]} rows={Object.entries(preview.impact.by_classification).map(([c, n]) => ({ c, n }))} /></Card><Card title="By object type"><Table cols={[{ k: "t", h: "Type" }, { k: "d", h: "Classification counts", r: (r) => Object.entries(r.d).map(([k, v]) => `${k}: ${v}`).join(" · ") }]} rows={Object.entries(preview.impact.by_type).map(([t, d]) => ({ t, d }))} /></Card></div>
        <Card title="Expansion explanations (sample)"><Table cols={[{ k: "node", h: "Object" }, { k: "policy", h: "Policy", r: (r) => <Pill value={r.policy} /> }, { k: "reason", h: "Reason" }]} rows={preview.traces.slice(0, 60)} /></Card>
      </>}
      <Card title="Manifests (versioned, immutable once approved)" actions={<><select value={cmp.a} onChange={(e) => setCmp({ ...cmp, a: e.target.value })}><option value="">A</option>{(manifests.data || []).map((m) => <option key={m.id} value={m.id}>{m.name} v{m.version}</option>)}</select> <select value={cmp.b} onChange={(e) => setCmp({ ...cmp, b: e.target.value })}><option value="">B</option>{(manifests.data || []).map((m) => <option key={m.id} value={m.id}>{m.name} v{m.version}</option>)}</select> <button className="secondary" onClick={compare}>What-if compare</button></>}>
        <Table cols={[{ k: "name", h: "Name" }, { k: "version", h: "v" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "o", h: "Objects", r: (r) => fmtNum(r.impact.objects_total) }, { k: "b", h: "Volume", r: (r) => fmtBytes(r.impact.est_bytes) }, { k: "a", h: "Approvals", r: (r) => r.impact.approvals_required }, { k: "content_hash", h: "Hash", r: (r) => r.content_hash.slice(0, 12) }, { k: "created_by", h: "Created by" }, { k: "approved_by", h: "Approved by" }]} rows={manifests.data || []} />
        {cmpRes && <><h4>Comparison</h4><KV obj={{ only_in_a: cmpRes.only_in_a, only_in_b: cmpRes.only_in_b, reclassified: cmpRes.reclassified, delta_objects: cmpRes.delta_objects, delta_bytes: fmtBytes(Math.abs(cmpRes.delta_bytes)) + (cmpRes.delta_bytes < 0 ? " less" : " more") }} /><Table cols={[{ k: "node", h: "Object" }, { k: "a", h: "A", r: (r) => <Pill value={r.a} /> }, { k: "b", h: "B", r: (r) => <Pill value={r.b} /> }]} rows={cmpRes.samples.reclassified} /></>}
      </Card>
    </div>
  );
}

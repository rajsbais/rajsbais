import { useState } from "react";
import { api, fmtNum, getToken } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, KV, Pill, Select, Table } from "../components/ui";

export default function RunsMonitor() {
  const { projectId } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const runs = useApi<any[]>(projectId ? `/projects/${projectId}/runs` : null);
  const [mid, setMid] = useState(""); const [rid, setRid] = useState(""); const [workers, setWorkers] = useState(4);
  const [execution, setExecution] = useState("INLINE"); const [staging, setStaging] = useState(""); const [pipelined, setPipelined] = useState(true); const [loadMode, setLoadMode] = useState("api");
  const [sel, setSel] = useState("");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<string | null>(null);
  const run = (runs.data || []).find((r) => r.id === (sel || runs.data?.[0]?.id));
  const staged = useApi<any>(run ? `/runs/${run.id}/staged` : null, { limit: 20 }, [run?.id]);
  const jobs = useApi<any>(run?.metrics?.execution === "DISTRIBUTED" ? `/runs/${run.id}/jobs` : null, undefined, [run?.id, run?.status]);
  const workersQ = useApi<any>("/platform/workers", undefined, [run?.status]);
  const requeue = async () => { setErr(null); try { await api(`/runs/${run.id}/jobs/requeue`, { method: "POST" }); jobs.reload(); } catch (e: any) { setErr(e.message); } };
  const start = async () => { setBusy(true); setErr(null); try { const r = await api(`/projects/${projectId}/runs`, { body: { manifest_id: mid, ruleset_id: rid, mode: "SIMULATED", workers, execution, staging_backend: staging || null, pipelined, load_mode: loadMode } }); runs.reload(); setSel(r.id); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const cockpit = useApi<any>(run ? `/runs/${run.id}/cockpit-export` : null, undefined, [run?.id, run?.status]);
  const [exporting, setExporting] = useState(false);
  const exportCockpit = async () => { setExporting(true); setErr(null); try { await api(`/runs/${run.id}/cockpit-export`, { method: "POST", body: { formats: ["csv", "xml"] } }); cockpit.reload(); } catch (e: any) { setErr(e.message); } finally { setExporting(false); } };
  const downloadCockpit = async () => {
    setErr(null);
    try {
      const base = (import.meta.env.VITE_API_BASE as string | undefined) || "/api/v1";
      const res = await fetch(`${base}/runs/${run.id}/cockpit-export/download`, { headers: { Authorization: `Bearer ${getToken() || ""}` } });
      if (!res.ok) throw new Error(`download failed: ${res.status}`);
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a"); a.href = url; a.download = `cockpit_${run.id}.zip`; a.click(); URL.revokeObjectURL(url);
    } catch (e: any) { setErr(e.message); }
  };
  const templates = useApi<any[]>(projectId ? `/projects/${projectId}/cockpit-templates` : null, undefined, [projectId]);
  const samples = useApi<any>("/cockpit-templates/samples");
  const [tplObject, setTplObject] = useState("");
  const [tplBusy, setTplBusy] = useState(false);
  const registerTemplate = async (content: string, filename: string) => {
    if (!tplObject) { setErr("choose the business object the template belongs to"); return; }
    setTplBusy(true); setErr(null);
    try { await api(`/projects/${projectId}/cockpit-templates`, { body: { object_type: tplObject, filename, content } }); templates.reload(); aliases.reload(); } catch (e: any) { setErr(e.message); } finally { setTplBusy(false); }
  };
  const uploadTemplate = async (e: React.ChangeEvent<HTMLInputElement>) => { const f = e.target.files?.[0]; if (!f) return; await registerTemplate(await f.text(), f.name); e.target.value = ""; };
  const useSample = async () => {
    const base = (import.meta.env.VITE_API_BASE as string | undefined) || "/api/v1";
    setTplBusy(true); setErr(null);
    try {
      const res = await fetch(`${base}/cockpit-templates/samples/${tplObject}`, { headers: { Authorization: `Bearer ${getToken() || ""}` } });
      if (!res.ok) throw new Error(`no sample template for ${tplObject}`);
      await registerTemplate(await res.text(), `${tplObject}.sample-template.xml`);
    } catch (e: any) { setErr(e.message); } finally { setTplBusy(false); }
  };
  const migObjects = useApi<any>(projectId ? `/projects/${projectId}/migration-objects` : null, undefined, [projectId]);
  const [moImport, setMoImport] = useState("");
  const importMigrationObjects = async () => {
    setErr(null);
    try { const entries = JSON.parse(moImport); await api(`/projects/${projectId}/migration-objects/import`, { body: { entries: Array.isArray(entries) ? entries : entries.entries, release: Array.isArray(entries) ? null : entries.release || null, source: "pasted on the Runs page" } }); setMoImport(""); migObjects.reload(); cockpit.reload(); } catch (e: any) { setErr(e.message); }
  };
  const deleteMigrationObject = async (e: any) => { setErr(null); try { await api(`/projects/${projectId}/migration-objects/${e.id}`, { method: "DELETE" }); migObjects.reload(); } catch (x: any) { setErr(x.message); } };
  const aliases = useApi<any[]>(projectId ? `/projects/${projectId}/cockpit-aliases` : null, undefined, [projectId]);
  const decideAlias = async (a: any, status: string) => { setErr(null); try { await api(`/projects/${projectId}/cockpit-aliases/${a.id}/decide`, { body: { status } }); aliases.reload(); templates.reload(); } catch (e: any) { setErr(e.message); } };
  const proposeAliases = async () => { setErr(null); try { await api(`/projects/${projectId}/cockpit-aliases/propose`, { method: "POST" }); aliases.reload(); } catch (e: any) { setErr(e.message); } };
  const deleteTemplate = async (t: any) => { setErr(null); try { await api(`/projects/${projectId}/cockpit-templates/${t.id}`, { method: "DELETE" }); templates.reload(); aliases.reload(); } catch (e: any) { setErr(e.message); } };
  const resume = async () => { setBusy(true); setErr(null); try { await api(`/runs/${run.id}/resume`, { method: "POST" }); runs.reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  if (!projectId) return <Banner>Select a project.</Banner>;
  return (
    <div>
      <Banner kind="warn">Runs are <b>SIMULATED</b>: extraction reads the synthetic record store or the simulated SAP add-on over RFC; the load goes through the released S/4HANA APIs on the simulated gateway (business partner, product, sales/purchase order, journal entry; migration cockpit for histories and cockpit objects) or, in direct mode, into the simulated target. Production and rehearsal modes are refused by the engine.</Banner>
      <Card title="Start migration run" actions={<><Select value={mid} onChange={setMid} options={(manifests.data || []).filter((m) => m.status === "APPROVED").map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} placeholder="approved manifest" /><Select value={rid} onChange={setRid} options={(rulesets.data || []).filter((r) => r.status === "APPROVED").map((r) => ({ value: r.id, label: `${r.name} v${r.version}` }))} placeholder="approved ruleset" /><label className="chk">workers <input type="number" min={1} max={16} value={workers} onChange={(e) => setWorkers(Number(e.target.value))} style={{ width: 60 }} /></label><select value={execution} onChange={(e) => setExecution(e.target.value)}><option value="INLINE">INLINE (threads in API)</option><option value="DISTRIBUTED">DISTRIBUTED (worker pods)</option></select><select value={staging} onChange={(e) => setStaging(e.target.value)}><option value="">staging: default</option><option value="relational">relational</option><option value="columnar">columnar (Parquet)</option></select><select value={loadMode} onChange={(e) => setLoadMode(e.target.value)} title="api: released S/4HANA APIs over the target transport (simulated gateway or HTTPS); direct: simulated direct loader"><option value="api">load: released APIs</option><option value="direct">load: direct (simulated)</option></select>{execution === "DISTRIBUTED" && <label className="chk"><input type="checkbox" checked={pipelined} onChange={(e) => setPipelined(e.target.checked)} /> pipelined</label>}<button disabled={busy || !mid || !rid} onClick={start}>{busy ? "Running…" : "Start simulated run"}</button></>}><ErrorBox error={err} /><p className="muted">Only APPROVED manifests and rulesets are offered. The engine re-verifies approval state and manifest hash before execution.</p></Card>
      <div className="grid2">
        <Card title="Runs"><Table cols={[{ k: "id", h: "Run", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setSel(r.id); }}>{r.id.slice(0, 8)}</a> }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "reconciliation", h: "Recon", r: (r) => <Pill value={r.reconciliation} /> }, { k: "started_by", h: "By" }, { k: "started_at", h: "Started", r: (r) => String(r.started_at || "").slice(0, 19) }]} rows={runs.data || []} /></Card>
        {run && <Card title={`Run ${run.id.slice(0, 8)} stages`} actions={run.status === "FAILED" && <button disabled={busy} onClick={resume}>Resume from checkpoint</button>}>
          <Table cols={[{ k: "name", h: "Stage" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "duration_ms", h: "ms" }, { k: "m", h: "Metrics", r: (r) => Object.entries(r.metrics || {}).filter(([, v]) => typeof v !== "object").map(([k, v]) => `${k}=${v}`).join(" · ") }]} rows={run.stages} />
          <KV obj={{ snapshot: run.snapshot_id, mode: run.mode, execution: run.metrics?.execution, pipelined: run.metrics?.pipelined, pipeline_overlap_s: run.metrics?.pipeline_overlap_s, staging_backend: run.metrics?.staging_backend, workers: run.metrics?.workers }} />
        </Card>}
      </div>
      {run && jobs.data && <Card title={`Stage jobs (${jobs.data.total}) — ${Object.entries(jobs.data.by_stage || {}).map(([st, m]: any) => `${st}: ${Object.entries(m).map(([k, v]) => `${v} ${k}`).join("/")}`).join(" · ")}`} actions={<><span className="muted">queue: {workersQ.data?.queued_jobs ?? "-"} queued · workers seen: {(workersQ.data?.workers || []).map((w: any) => w.worker).join(", ") || "none"}</span> <button className="secondary" onClick={() => { jobs.reload(); workersQ.reload(); runs.reload(); }}>Refresh</button> <button className="secondary" onClick={requeue}>Re-queue failed / expired</button></>}>
        <p className="muted">Start worker processes with <code>sdtf worker</code> (or scale the worker Deployment). Extraction, transformation and load run as per-partition jobs with stage barriers; jobs are leased; expired leases are re-queued; the worker that completes a stage advances the run.</p>
        <Table cols={[{ k: "stage", h: "Stage" }, { k: "partition", h: "Partition" }, { k: "object_type", h: "Object" }, { k: "status", h: "Status", r: (j) => <Pill value={j.status} /> }, { k: "worker", h: "Worker" }, { k: "attempts", h: "Attempts" }, { k: "records", h: "Records" }, { k: "error", h: "Error" }]} rows={jobs.data.jobs} />
      </Card>}
      {run && (run.status === "COMPLETED" || run.status === "FAILED") && <Card title="Migration cockpit staging files" actions={<><button className="secondary" disabled={exporting} onClick={exportCockpit}>{exporting ? "Exporting…" : cockpit.data?.exported ? "Re-export" : "Export cockpit files"}</button>{cockpit.data?.exported && <button onClick={downloadCockpit}>Download zip</button>}</>}>
        <p className="muted">Rows the initial load routes to the migration cockpit (cockpit objects, tables the document APIs do not expose, histories) as one CSV per staging table and one SpreadsheetML workbook per migration object, with a manifest of checksums. A real target loads them through the <i>Migrate Your Data</i> app; the files are not generated from the target's own templates and migration object names are hints to verify.</p>
        {cockpit.data?.exported ? <>
          <KV obj={{ rows: fmtNum(cockpit.data.rows), files: cockpit.data.files, generated: cockpit.data.generated_at, manifest_sha256: String(cockpit.data.manifest_sha256).slice(0, 16) + "…", directory: cockpit.data.dir }} />
          <Table cols={[{ k: "object", h: "Business object" }, { k: "migration_object", h: "Migration object", r: (o) => <span title={o.migration_object_lookup ? `${o.migration_object_lookup.source}: ${o.migration_object_lookup.confidence}. ${o.migration_object_lookup.note || ""}` : ""}>{o.migration_object} {o.migration_object_lookup && <Pill value={o.migration_object_lookup.status === "found" ? (o.migration_object_lookup.source === "catalogue" ? "DOCUMENTED" : "IMPORTED") : o.migration_object_lookup.status === "candidates" ? "CANDIDATES" : "NONE"} />}</span> }, { k: "tables", h: "Tables", r: (o) => o.tables.join(", ") }, { k: "rows", h: "Rows", r: (o) => fmtNum(o.rows) }, { k: "load_status", h: "Load status", r: (o) => Object.entries(o.load_status || {}).map(([k, v]) => `${v} ${k}`).join(" · ") }, { k: "reasons", h: "Why the cockpit", r: (o) => o.reasons.join("; ") }]} rows={Object.entries(cockpit.data.objects || {}).map(([object, o]: any) => ({ object, ...o }))} />
        </> : <p className="muted">No export yet for this run.</p>}
        <h4>Migration objects of the target release {migObjects.data?.release ? `(${migObjects.data.release}${migObjects.data.normalized_release ? ", " + migObjects.data.normalized_release : ""})` : ""}</h4>
        <p className="muted">Which migration object each business object's files are for, resolved for the target's release: entries imported from the target's object list first, then the catalogue of documented objects (names per release with their renames; technical IDs are hints to confirm in the app). Paste the app's object list as JSON <code>[{"{"}"name": "G/L account", "id": "SIF_GL_ACCOUNT", "release": "2023", "object_types": ["FI.GLAccount"]{"}"}]</code> to make the lookup authoritative for this project.</p>
        <Table cols={[{ k: "object", h: "Business object" }, { k: "status", h: "", r: (r) => <Pill value={r.status === "found" ? (r.source === "catalogue" ? "DOCUMENTED" : "IMPORTED") : r.status.toUpperCase()} /> }, { k: "name", h: "Migration object", r: (r) => r.name || (r.candidates || []).map((c: any) => c.name).join(" / ") || "-" }, { k: "id", h: "ID", r: (r) => r.id || "-" }, { k: "confidence", h: "Confidence" }, { k: "note", h: "Note" }]} rows={Object.entries(migObjects.data?.objects || {}).map(([object, r]: any) => ({ object, ...r }))} />
        <div className="row"><textarea value={moImport} onChange={(e) => setMoImport(e.target.value)} placeholder='[{"name": "...", "id": "SIF_...", "release": "2023", "object_types": ["FI.GLAccount"], "tables": ["SKA1", "SKB1"]}]' rows={3} style={{ width: "100%" }} /><button className="secondary" disabled={!moImport.trim()} onClick={importMigrationObjects}>Import object list</button></div>
        {(migObjects.data?.registry || []).length > 0 && <Table cols={[{ k: "release", h: "Release" }, { k: "name", h: "Name" }, { k: "object_id", h: "ID" }, { k: "object_types", h: "Business objects", r: (e) => (e.object_types || []).join(", ") }, { k: "tables", h: "Tables", r: (e) => (e.tables || []).join(", ") }, { k: "source", h: "Source" }, { k: "x", h: "", r: (e) => <button className="secondary" onClick={() => deleteMigrationObject(e)}>Remove</button> }]} rows={migObjects.data.registry} />}
        <h4>Migration object templates of the target</h4>
        <p className="muted">Register the XML template the <i>Migrate Your Data</i> app provides for a migration object; the export then fills it (<code>&lt;OBJECT&gt;.template.xml</code>) through an automatic mapping (same technical names, BAPI-style aliases, parent keys) plus your recorded overrides. The report shows the coverage and the mandatory fields still unmapped. Illustrative samples exist for a few objects; they are not SAP files.</p>
        <div className="row">
          <Select value={tplObject} onChange={setTplObject} options={Array.from(new Set([...Object.keys(cockpit.data?.objects || {}), ...(samples.data?.objects || [])])).sort().map((o) => ({ value: o, label: o }))} placeholder="business object" />
          <label className="chk">template file <input type="file" accept=".xml,text/xml,application/xml" disabled={tplBusy || !tplObject} onChange={uploadTemplate} /></label>
          {(samples.data?.objects || []).includes(tplObject) && <button className="secondary" disabled={tplBusy} onClick={useSample}>Use illustrative sample</button>}
        </div>
        <Table cols={[{ k: "object_type", h: "Business object" }, { k: "migration_object", h: "Migration object" }, { k: "filename", h: "Template" }, { k: "sheets", h: "Sheets", r: (t) => (t.sheets || []).map((s: any) => `${s.name} (${s.fields})`).join(", ") }, { k: "coverage", h: "Mapped", r: (t) => `${t.report.mapped}/${t.report.total} (${Math.round(t.report.coverage * 100)}%)` }, { k: "proposals", h: "Alias proposals", r: (t) => (t.alias_proposals || []).length }, { k: "missing", h: "Mandatory unmapped", r: (t) => Object.entries(t.report.mandatory_missing || {}).map(([s, f]: any) => `${s}: ${f.join(", ")}`).join("; ") || "-" }, { k: "check", h: "Layout check", r: (t) => t.check?.documented_layout ? <Pill value="DOCUMENTED" /> : <span title={(t.check?.warnings || []).join("\n")}><Pill value="DEVIATES" /> {(t.check?.warnings || []).length} warning(s)</span> }, { k: "x", h: "", r: (t) => <button className="secondary" onClick={() => deleteTemplate(t)}>Remove</button> }]} rows={templates.data || []} empty="No templates registered for this project." />
        <h4>Aliases learned from the templates' Field Lists</h4>
        <p className="muted">A template names fields its own way. Names the global catalogue (BAPI-style names from the public interface structures) does not know are proposed from the Field List: a field of the sheet's table whose DDIC description equals the template's field description. Confirm or reject each proposal; confirmed aliases apply to every template of this project and are consulted before the global catalogue. Entries marked "confirm or reject" are global aliases whose DDIC description disagrees with the Field List.</p>
        <div className="row"><button className="secondary" onClick={proposeAliases}>Re-read Field Lists</button></div>
        <Table cols={[{ k: "status", h: "Status", r: (a) => <Pill value={a.status} /> }, { k: "alias", h: "Template field" }, { k: "description", h: "Field List description" }, { k: "field", h: "DDIC field", r: (a) => `${a.table ? a.table + "." : ""}${a.field}${a.ddic_description ? " (" + a.ddic_description + ")" : ""}` }, { k: "evidence", h: "Evidence" }, { k: "x", h: "", r: (a) => a.status === "PROPOSED" ? <><button onClick={() => decideAlias(a, "CONFIRMED")}>Confirm</button> <button className="secondary" onClick={() => decideAlias(a, "REJECTED")}>Reject</button></> : a.status === "REJECTED" ? <button className="secondary" onClick={() => decideAlias(a, "CONFIRMED")}>Confirm after all</button> : null }]} rows={aliases.data || []} empty="No aliases learned yet: register a template whose Field List names fields differently from the catalogue." />
      </Card>}
      {run && staged.data && <div className="grid2"><Card title="Staging by table and load status"><Table cols={[{ k: "table", h: "Table" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "count", h: "Records", r: (r) => fmtNum(r.count) }]} rows={staged.data.counts} /></Card><Card title="Staged record samples with lineage"><Table cols={[{ k: "table", h: "Table" }, { k: "key", h: "Source key" }, { k: "target_key", h: "Target key" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "lineage", h: "Lineage", r: (r) => r.lineage.map((l: any) => `${l.rule}:${l.field} ${l.from}→${l.to}`).join("; ") }]} rows={staged.data.items} /></Card></div>}
    </div>
  );
}

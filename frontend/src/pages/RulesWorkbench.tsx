import { useEffect, useState } from "react";
import { api } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, List, Pill, Pre, Select, Table, Tabs } from "../components/ui";

export default function RulesWorkbench() {
  const { projectId, source } = useProjectDetails();
  const manifests = useApi<any[]>(projectId ? `/projects/${projectId}/manifests` : null);
  const rulesets = useApi<any[]>(projectId ? `/projects/${projectId}/rulesets` : null);
  const [mid, setMid] = useState("");
  const [yaml, setYaml] = useState("");
  const [val, setVal] = useState<any>(null);
  const [rid, setRid] = useState("");
  const [dry, setDry] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [bukrs, setBukrs] = useState("5000");
  const guard = async (fn: () => Promise<void>) => { setErr(null); try { await fn(); } catch (e: any) { setErr(e.message); } };
  const generate = () => guard(async () => { const r = await api(`/projects/${projectId}/rulesets/generate`, { method: "POST", params: { manifest_id: mid || manifests.data?.[0]?.id } }); setYaml(r.source_yaml); setVal(null); });
  const validate = () => guard(async () => setVal(await api("/rulesets/validate", { body: { source_yaml: yaml } })));
  const save = () => guard(async () => { const r = await api(`/projects/${projectId}/rulesets`, { body: { source_yaml: yaml } }); rulesets.reload(); setRid(r.id); setVal(r.validation); });
  const dryRun = () => guard(async () => setDry(await api(`/rulesets/${rid}/dry-run`, { body: { system_id: source.id, bukrs, tables: ["BKPF", "BSEG", "KNA1", "KNB1", "LFA1", "VBAK", "EKKO", "MARC", "CSKS"], sample_size: 40 } })));
  const approve = () => guard(async () => { await api(`/rulesets/${rid}/approve`, { body: {} }); rulesets.reload(); });
  const load = (id: string) => guard(async () => { setRid(id); const r = await api(`/rulesets/${id}`); setYaml(r.source_yaml); setVal(r.validation); setRules(r.compiled?.rules || r.rules || []); });
  const [rules, setRules] = useState<any[]>([]);
  useEffect(() => { if (!rid && rulesets.data?.length) load(rulesets.data[0].id); }, [rulesets.data]);
  if (!projectId || !source) return <Banner>Select a project.</Banner>;
  const current = (rulesets.data || []).find((r) => r.id === rid);
  const describe = (r: any) => { const map = r.map || r.mapping || {}; const first = Object.entries(map)[0]; return `${r.id || r.name || r.type}: ${r.type || ""}${r.field ? ` ${r.field}` : ""}${first ? ` ${first[0]} → ${first[1]}${Object.keys(map).length > 1 ? ` (+${Object.keys(map).length - 1})` : ""}` : r.prefix ? ` prefix ${r.prefix}` : r.value !== undefined ? ` = ${r.value}` : ""}`; };
  const lines = (which: "before" | "after") => Object.entries(dry?.tables || {}).flatMap(([t, d]: any) => d.samples.slice(0, 4).map((smp: any) => { const r = smp[which] || {}; return `${t} ${["BUKRS", "BELNR", "HKONT", "KUNNR", "LIFNR", "MATNR", "WERKS", "DMBTR"].filter((k) => r[k] !== undefined).map((k) => r[k]).join(" ")}`; }));
  return (
    <div>
      <Card title="Rules of the current set" actions={current && <><Pill value={current.status} /> <span className="muted">{current.name} v{current.version}</span></>}>
        <List items={rules.slice(0, 20).map((r, i) => ({ id: String(i), k: describe(r) }))} empty="No rule set yet: generate candidate rules from a manifest below, validate and save." />
        {rules.length > 20 && <p className="muted">{rules.length - 20} more rules in the YAML below.</p>}
        <div className="row" style={{ marginTop: 14 }}><button disabled={!rid} onClick={dryRun}>Dry-run on the source sample</button><input value={bukrs} onChange={(e) => setBukrs(e.target.value)} style={{ width: 110 }} placeholder="company code" /><button className="secondary" disabled={!current || current.status !== "DRAFT" || !current.validation?.ok} onClick={approve}>Approve (four-eyes)</button></div>
        {!rid && <p className="note-danger">Save a rule set first. Rules do not run on an empty package.</p>}
        {dry && <div className="grid2" style={{ marginTop: 12 }}><Card title="Source lines"><Pre value={lines("before").join("\n")} /></Card><Card title="Preview"><Pre value={lines("after").join("\n")} /></Card></div>}
        {dry && <p className="muted">{dry.records} records sampled · {dry.changed} changed · {dry.exceptions.length} exceptions. Amounts are never rewritten by organisational rules; the full lineage is under Dry run &amp; impact.</p>}
      </Card>
      <Card title="Rule editor (declarative YAML DSL)" actions={<><Select value={mid} onChange={setMid} options={(manifests.data || []).map((m) => ({ value: m.id, label: `${m.name} v${m.version}` }))} placeholder="manifest for generation" /><button className="secondary" onClick={generate}>Generate candidate rules (Rule Factory)</button> <button className="secondary" onClick={validate} disabled={!yaml}>Validate + run embedded tests</button> <button onClick={save} disabled={!yaml}>Save as new version</button></>}>
        <textarea className="code" value={yaml} onChange={(e) => setYaml(e.target.value)} placeholder="ruleset: my-rules&#10;rules:&#10;  - id: cc&#10;    type: org_reassign&#10;    field: BUKRS&#10;    map: {'5000': 'SP01'}" />
        <ErrorBox error={err} />
        {val && <div style={{ marginTop: 10 }}><Pill value={val.ok ? "VALID" : "INVALID"} /> {val.rule_count} rules · tests {val.tests.passed} passed / {val.tests.failed} failed{val.errors.map((e: string) => <div key={e} className="banner danger">{e}</div>)}{val.warnings.map((w: string) => <div key={w} className="banner warn">{w}</div>)}</div>}
      </Card>
      <Tabs tabs={[
        { id: "rs", label: "Rule sets", content: <Card actions={current && <><Pill value={current.status} /> <button disabled={current.status !== "DRAFT" || !current.validation?.ok} onClick={approve}>Approve (four-eyes)</button></>}><Table cols={[{ k: "name", h: "Name", r: (r) => <a href="#" onClick={(e) => { e.preventDefault(); load(r.id); }}>{r.name}</a> }, { k: "version", h: "v" }, { k: "status", h: "Status", r: (r) => <Pill value={r.status} /> }, { k: "rule_count", h: "Rules" }, { k: "v", h: "Valid", r: (r) => <Pill value={r.validation?.ok ? "VALID" : "INVALID"} /> }, { k: "content_hash", h: "Hash", r: (r) => r.content_hash.slice(0, 12) }, { k: "created_by", h: "Created by" }, { k: "approved_by", h: "Approved by" }]} rows={rulesets.data || []} /></Card> },
        { id: "dry", label: "Dry run & impact", content: <Card actions={<><input value={bukrs} onChange={(e) => setBukrs(e.target.value)} style={{ width: 90 }} /><button disabled={!rid} onClick={dryRun}>Dry run on source sample</button></>}>{dry && <><p>{dry.records} records sampled · {dry.changed} changed · {dry.exceptions.length} exceptions</p><div className="grid2"><div><h4>Impact per rule</h4><Table cols={[{ k: "rule", h: "Rule" }, { k: "n", h: "Field changes" }]} rows={Object.entries(dry.rule_impact).map(([rule, n]) => ({ rule, n }))} /><h4>Exceptions</h4><Table cols={[{ k: "table", h: "Table" }, { k: "key", h: "Key" }, { k: "rule", h: "Rule" }, { k: "message", h: "Message" }]} rows={dry.exceptions} /></div><div><h4>Before / after samples</h4>{Object.entries(dry.tables).map(([t, d]: any) => d.samples.slice(0, 2).map((s: any, i: number) => <div key={t + i}><b>{t}</b> lineage: {s.lineage.map((l: any) => `${l.rule}: ${l.field} ${l.from}→${l.to}`).join("; ")}<Pre value={{ before: s.before, after: s.after }} /></div>))}</div></div></>}</Card> },
      ]} />
    </div>
  );
}

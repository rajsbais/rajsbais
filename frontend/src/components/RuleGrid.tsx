import { useState } from "react";
import { Pill, Table } from "./ui";

export const RULE_TYPES = ["org_reassign", "value_map", "key_map", "number_range", "currency_convert", "default", "conditional", "field_map", "lookup_enrich", "reject", "skip"];
const ON_MISSING = ["passthrough", "error", "default"];

export type RuleDoc = { ruleset: string; version?: number; description?: string; applies_to?: any; lookups: Record<string, Record<string, string>>; rules: any[]; tests?: any[] };

/** "key = value" lines <-> object; the grid edits mappings as text so a 200-entry map stays readable. */
export function linesToMap(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  text.split("\n").forEach((l) => { const m = l.match(/^\s*([^=:]+?)\s*(?:=|:|→|->)\s*(.*?)\s*$/); if (m && m[1]) out[m[1]] = m[2]; });
  return out;
}
export function mapToLines(m: Record<string, any> | undefined): string { return Object.entries(m || {}).map(([k, v]) => `${k} = ${v}`).join("\n"); }
const list = (v: any): string => Array.isArray(v) ? v.join(", ") : (v || "");
const splitList = (s: string): string[] => s.split(",").map((x) => x.trim()).filter(Boolean);

/** Words for a rule, the same wording the grid endpoint uses, for rules edited but not yet saved. */
export function describeRule(r: any, lookups: Record<string, any> = {}): { fields: string; mapping: string; condition: string } {
  const t = r.type;
  const map = r.map || {};
  const n = Object.keys(map).length;
  const fields = t === "default" ? Object.keys(r.set || {}).join(", ") : t === "field_map" ? Object.keys(map).join(", ") : t === "lookup_enrich" ? r.set_field || "" : t === "conditional" ? Object.keys(r.then?.set || {}).join(", ") : t === "org_reassign" ? [r.field || "BUKRS", ...(r.also_fields || [])].join(", ") : list(r.fields || (r.field ? [r.field] : []));
  let mapping = "";
  if (t === "lookup_enrich") mapping = `${r.set_field || "?"} from ${r.lookup || "?"} by ${r.key_field || "?"}`;
  else if (n && t !== "field_map") mapping = Object.entries(map).slice(0, 3).map(([a, b]) => `${a} → ${b}`).join(", ") + (n > 3 ? ` (+${n - 3})` : "");
  else if (r.lookup) mapping = `lookup ${r.lookup} (${Object.keys(lookups[r.lookup] || {}).length} entries)`;
  else if (t === "key_map") mapping = `${r.strategy || "prefix"} ${r.prefix || r.offset || ""}`.trim();
  else if (t === "number_range") mapping = `offset ${r.offset || 0}${r.prefix ? `, prefix ${r.prefix}` : ""}`;
  else if (t === "currency_convert") mapping = `to ${r.to || "?"} (${Object.keys(r.rates || {}).length} rates)`;
  else if (t === "default") mapping = Object.entries(r.set || {}).map(([a, b]) => `${a} = ${b}`).join(", ");
  else if (t === "conditional") mapping = Object.entries(r.then?.set || {}).map(([a, b]) => `${a} = ${b}`).join(", ");
  else if (t === "field_map") mapping = Object.entries(map).map(([a, b]) => `${a} → ${b}`).join(", ");
  else if (t === "reject" || t === "skip") mapping = r.message || "";
  return { fields, mapping, condition: whenText(r.when) };
}
export function whenText(w: any): string {
  if (!w) return "";
  if (w.all) return w.all.map(whenText).join(" and ");
  if (w.any) return w.any.map(whenText).join(" or ");
  const f = w.field || "?";
  for (const op of ["equals", "in", "not_in", "present", "prefix", "not_prefix", "in_lookup", "not_in_lookup"]) if (op in w) return `${f} ${op.replace("_", " ")} ${Array.isArray(w[op]) ? JSON.stringify(w[op]) : w[op]}`;
  return JSON.stringify(w);
}

function RuleForm({ rule, lookups, onChange, onClose }: { rule: any; lookups: Record<string, any>; onChange: (r: any) => void; onClose: () => void }) {
  const [whenErr, setWhenErr] = useState<string | null>(null);
  const set = (k: string, v: any) => onChange({ ...rule, [k]: v });
  const t = rule.type;
  const text = (k: string, label: string, ph = "") => <label>{label}<input value={rule[k] ?? ""} onChange={(e) => set(k, e.target.value)} placeholder={ph} /></label>;
  const lst = (k: string, label: string) => <label>{label}<input value={list(rule[k])} onChange={(e) => set(k, splitList(e.target.value))} placeholder="comma-separated" /></label>;
  const mapArea = (k: string, label: string) => <label>{label}<textarea className="code" style={{ minHeight: 90 }} defaultValue={mapToLines(rule[k])} onBlur={(e) => set(k, linesToMap(e.target.value))} placeholder={"5000 = SP01\n5100 = SP02"} /></label>;
  const usesMap = ["org_reassign", "value_map"].includes(t);
  return (
    <div className="rc-form" style={{ marginTop: 12, borderTop: "1px solid var(--border)", paddingTop: 10 }}>
      <div className="row">
        {text("id", "Rule id")}
        <label>Type<select value={t} onChange={(e) => set("type", e.target.value)}>{RULE_TYPES.map((x) => <option key={x} value={x}>{x}</option>)}</select></label>
        <label>Tables (glob, comma-separated)<input value={list(rule.tables)} onChange={(e) => set("tables", splitList(e.target.value))} placeholder="* or BKPF, BSEG" /></label>
      </div>
      {text("description", "Description", "what the rule does, for the approver")}
      {t === "org_reassign" && <div className="row">{text("field", "Field", "BUKRS")}{lst("also_fields", "Also fields")}</div>}
      {["value_map", "key_map", "number_range", "currency_convert"].includes(t) && lst("fields", "Fields")}
      {usesMap && mapArea("map", "Mapping (one `source = target` per line; leave empty to use a lookup)")}
      {(usesMap || t === "key_map" || t === "lookup_enrich") && <label>Lookup<select value={rule.lookup || ""} onChange={(e) => set("lookup", e.target.value || undefined)}><option value="">(none)</option>{Object.keys(lookups).map((n) => <option key={n} value={n}>{n} ({Object.keys(lookups[n]).length})</option>)}</select></label>}
      {(usesMap || t === "key_map" || t === "lookup_enrich") && <div className="row"><label>On missing<select value={rule.on_missing || ""} onChange={(e) => set("on_missing", e.target.value || undefined)}><option value="">(default)</option>{ON_MISSING.map((x) => <option key={x} value={x}>{x}</option>)}</select></label>{rule.on_missing === "default" && text("default", "Default value")}</div>}
      {t === "key_map" && <div className="row"><label>Strategy<select value={rule.strategy || "prefix"} onChange={(e) => set("strategy", e.target.value)}>{["prefix", "offset", "lookup"].map((x) => <option key={x} value={x}>{x}</option>)}</select></label>{text("prefix", "Prefix", "BP")}<label>Offset<input type="number" value={rule.offset ?? ""} onChange={(e) => set("offset", e.target.value === "" ? undefined : Number(e.target.value))} /></label>{text("emit_field", "Emit field", "PARTNER")}</div>}
      {t === "number_range" && <div className="row"><label>Offset<input type="number" value={rule.offset ?? ""} onChange={(e) => set("offset", e.target.value === "" ? undefined : Number(e.target.value))} /></label>{text("prefix", "Prefix")}</div>}
      {t === "currency_convert" && <><div className="row">{text("currency_field", "Currency field", "WAERS")}{text("to", "Target currency", "EUR")}</div>{mapArea("rates", "Rates (`USD->EUR = 0.92` per line)")}</>}
      {t === "default" && <>{mapArea("set", "Set (one `field = value` per line)")}<label className="chk"><input type="checkbox" checked={!!rule.overwrite} onChange={(e) => set("overwrite", e.target.checked || undefined)} /> overwrite existing values</label></>}
      {t === "conditional" && <label>Then set (one `field = value` per line)<textarea className="code" style={{ minHeight: 70 }} defaultValue={mapToLines(rule.then?.set)} onBlur={(e) => set("then", { ...(rule.then || {}), set: linesToMap(e.target.value) })} /></label>}
      {t === "field_map" && <>{mapArea("map", "Rename (`source = target` per line)")}<label className="chk"><input type="checkbox" checked={rule.drop_source !== false} onChange={(e) => set("drop_source", e.target.checked ? undefined : false)} /> drop the source field</label></>}
      {t === "lookup_enrich" && <div className="row">{text("key_field", "Key field", "KOSTL")}{text("set_field", "Set field", "PRCTR")}</div>}
      {["reject", "skip"].includes(t) && text("message", "Message")}
      <label>Condition `when` (JSON; equals, in, not_in, present, prefix, not_prefix, in_lookup, all / any)<textarea className="code" style={{ minHeight: 60 }} defaultValue={rule.when ? JSON.stringify(rule.when) : ""} onBlur={(e) => { const v = e.target.value.trim(); if (!v) { setWhenErr(null); set("when", undefined); return; } try { set("when", JSON.parse(v)); setWhenErr(null); } catch { setWhenErr("condition is not valid JSON"); } }} placeholder='{"field": "BSTAT", "in": ["S", "V"]}' /></label>
      {whenErr && <p className="note-danger">{whenErr}</p>}
      <div className="row"><button className="secondary" onClick={onClose}>Close editor</button></div>
    </div>
  );
}

export function RuleGrid({ doc, saved, impact, canDecide, editable, onChange, onDecide }: { doc: RuleDoc; saved: Record<string, any>; impact: Record<string, number>; canDecide: boolean; editable: boolean; onChange: (d: RuleDoc) => void; onDecide: (ruleId: string, decision: "APPROVED" | "REJECTED" | "PENDING") => void }) {
  const [editing, setEditing] = useState<number | null>(null);
  const update = (i: number, r: any) => onChange({ ...doc, rules: doc.rules.map((x, j) => (j === i ? r : x)) });
  const move = (i: number, d: number) => { const rules = [...doc.rules]; const j = i + d; if (j < 0 || j >= rules.length) return; [rules[i], rules[j]] = [rules[j], rules[i]]; onChange({ ...doc, rules }); setEditing(null); };
  const remove = (i: number) => { onChange({ ...doc, rules: doc.rules.filter((_, j) => j !== i) }); setEditing(null); };
  const add = () => { onChange({ ...doc, rules: [...doc.rules, { id: `rule-${doc.rules.length + 1}`, type: "value_map", tables: ["*"], fields: [], map: {}, on_missing: "passthrough" }] }); setEditing(doc.rules.length); };
  const rows = doc.rules.map((r, i) => { const d = describeRule(r, doc.lookups); const s = saved[r.id] || {}; return { i, id: r.id, description: r.description || "", type: r.type, tables: list(r.tables), ...d, impact: impact[r.id], decision: s.decision || "PENDING", by: s.decided_by ? `${s.decided_by}${s.carried_from ? ` (v${s.carried_from})` : ""}` : "", comment: s.comment || "" }; });
  return (
    <div>
      <Table cols={[
        { k: "i", h: "#", r: (r) => r.i + 1 },
        { k: "id", h: "Rule", r: (r) => <><b>{r.id}</b>{r.description && <div className="muted" style={{ fontSize: 12 }}>{r.description}</div>}</> },
        { k: "type", h: "Type", r: (r) => <code>{r.type}</code> },
        { k: "tables", h: "Tables" },
        { k: "fields", h: "Fields" },
        { k: "mapping", h: "Mapping / action" },
        { k: "condition", h: "Condition" },
        { k: "impact", h: "Dry-run changes", r: (r) => r.impact === undefined ? <span className="muted">–</span> : r.impact },
        { k: "decision", h: "Decision", r: (r) => <><Pill value={r.decision} />{r.by && <div className="muted" style={{ fontSize: 12 }}>{r.by}{r.comment ? ` · ${r.comment}` : ""}</div>}</> },
        { k: "a", h: "", r: (r) => <span className="grid-actions">
          {editable && <><button className="secondary" onClick={() => setEditing(editing === r.i ? null : r.i)}>{editing === r.i ? "Editing" : "Edit"}</button><button className="secondary" title="move up" onClick={() => move(r.i, -1)}>↑</button><button className="secondary" title="move down" onClick={() => move(r.i, 1)}>↓</button><button className="secondary" onClick={() => remove(r.i)}>Remove</button></>}
          {canDecide && r.decision !== "APPROVED" && <button onClick={() => onDecide(r.id, "APPROVED")}>Approve rule</button>}
          {canDecide && r.decision !== "REJECTED" && <button className="danger" onClick={() => onDecide(r.id, "REJECTED")}>Reject</button>}
          {canDecide && r.decision !== "PENDING" && <button className="secondary" onClick={() => onDecide(r.id, "PENDING")}>Reset</button>}
        </span> },
      ]} rows={rows} keyFn={(r) => String(r.i)} empty="No rules: generate candidate rules from a manifest, add a rule or paste YAML." />
      {editing !== null && doc.rules[editing] && <RuleForm rule={doc.rules[editing]} lookups={doc.lookups} onChange={(r) => update(editing, r)} onClose={() => setEditing(null)} />}
      {editable && <div className="row" style={{ marginTop: 10 }}><button className="secondary" onClick={add}>Add rule</button></div>}
    </div>
  );
}

export function LookupPanel({ doc, onChange, parseCsv }: { doc: RuleDoc; onChange: (d: RuleDoc) => void; parseCsv: (name: string, text: string, keyColumn: string, valueColumn: string) => Promise<any> }) {
  const [name, setName] = useState("");
  const [keyCol, setKeyCol] = useState("");
  const [valCol, setValCol] = useState("");
  const [text, setText] = useState("");
  const [rep, setRep] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const read = (f: File | undefined) => { if (!f) return; if (!name) setName(f.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]/g, "_")); f.text().then(setText); };
  const parse = async () => { setErr(null); setRep(null); try { setRep(await parseCsv(name, text, keyCol, valCol)); } catch (e: any) { setErr(e.message); } };
  const addLookup = () => { if (!rep?.entries) return; onChange({ ...doc, lookups: { ...doc.lookups, [rep.name]: rep.entries } }); setRep(null); setText(""); };
  const names = Object.keys(doc.lookups || {});
  return (
    <div>
      <Table cols={[{ k: "n", h: "Lookup" }, { k: "c", h: "Entries" }, { k: "u", h: "Used by" }, { k: "a", h: "", r: (r) => <button className="secondary" onClick={() => { const l = { ...doc.lookups }; delete l[r.n]; onChange({ ...doc, lookups: l }); }}>Remove</button> }]} rows={names.map((n) => ({ n, c: Object.keys(doc.lookups[n]).length, u: doc.rules.filter((r) => r.lookup === n || r.when?.in_lookup === n || r.when?.not_in_lookup === n).map((r) => r.id).join(", ") || "–" }))} empty="No lookup tables yet. Upload a two-column CSV (source, target) below." />
      <div className="rc-form" style={{ marginTop: 10 }}>
        <div className="row">
          <label>CSV file<input type="file" accept=".csv,.txt,.tsv" onChange={(e) => read(e.target.files?.[0])} /></label>
          <label>Lookup name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="coa_map" /></label>
          <label>Key column (optional)<input value={keyCol} onChange={(e) => setKeyCol(e.target.value)} placeholder="first column" /></label>
          <label>Value column (optional)<input value={valCol} onChange={(e) => setValCol(e.target.value)} placeholder="second column" /></label>
        </div>
        <label>Or paste the rows<textarea className="code" style={{ minHeight: 70 }} value={text} onChange={(e) => setText(e.target.value)} placeholder={"source;target\n140000;12100000"} /></label>
        <div className="row"><button className="secondary" disabled={!name || !text} onClick={parse}>Parse CSV</button>{rep && !rep.errors?.length && <button onClick={addLookup}>Add lookup “{rep.name}” ({Object.keys(rep.entries).length} entries)</button>}</div>
        {err && <p className="note-danger">{err}</p>}
        {rep && <div className="muted">{rep.rows} rows · {Object.keys(rep.entries || {}).length} entries · {rep.skipped} skipped · {rep.duplicates?.length || 0} duplicates · {rep.conflicts?.length || 0} conflicts · delimiter “{rep.delimiter === "\t" ? "tab" : rep.delimiter}” · {rep.header ? `header ${rep.key_column} → ${rep.value_column}` : "no header, first two columns"}{(rep.errors || []).map((e: string) => <div key={e} className="banner danger">{e}</div>)}</div>}
      </div>
    </div>
  );
}

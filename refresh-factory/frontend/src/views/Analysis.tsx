import { useEffect, useMemo, useState } from "react";
import { api, J } from "../api";
import { Badge, Bars, Card, DataTable, ErrorNote, Stat, TabList, TabPanel, useAction } from "../components";
import { useApp } from "../ctx";

const TABS = ["Distribution", "Selectivity", "Growth", "Tables and objects", "Not available"];
const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
const label = (v: Record<string, string>) => Object.values(v).join(" · ");

export default function Analysis() {
  const app = useApp();
  const [tab, setTab] = useState(0);
  const [sid, setSid] = useState("");
  const [cat, setCat] = useState<J>(null);
  const [table, setTable] = useState("");
  const [fields, setFields] = useState<string[]>([]);
  const [dateField, setDateField] = useState("");
  const [period, setPeriod] = useState("month");
  const [res, setRes] = useState<J>(null);
  const act = useAction();

  useEffect(() => { if (!sid && app.systems[0]) setSid((app.systems.find((s) => s.role === "PRD") ?? app.systems[0]).id); }, [app.systems.length]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (sid) { setCat(null); setRes(null); void act.run(async () => setCat(await api.get(`/api/systems/${sid}/analysis`))); } }, [sid]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setRes(null); }, [tab]);

  const tables: J[] = cat?.tables ?? [];
  const def = useMemo(() => tables.find((t) => t.table === table), [tables, table]);
  const personal: string[] = def?.personal_fields ?? [];
  const toggle = (f: string) => setFields((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : cur.length < (cat?.max_fields ?? 3) ? [...cur, f] : cur));
  const run = (kind: string) => act.run(async () => {
    const body = kind === "growth" ? { table, date_field: dateField, period } : { table, fields };
    setRes({ ...(await api.post(`/api/systems/${sid}/analysis/${kind}`, body)), kind });
  });
  const preset = (i: string, kind: "distribution" | "growth") => {
    const p = cat?.profiles?.[kind]?.[Number(i)];
    if (!p) return;
    setTable(p.table); setRes(null);
    if (kind === "distribution") setFields(p.fields); else setDateField(p.date_field);
  };

  const chooser = (kind: "distribution" | "selectivity" | "growth") => (
    <>
      <div className="form">
        <label>Table
          <select value={table} onChange={(e) => { setTable(e.target.value); setFields([]); setDateField(""); setRes(null); }}>
            <option value="">choose…</option>
            {tables.filter((t) => !t.customizing).map((t) => <option key={t.table} value={t.table}>{t.table} · {t.description}</option>)}
          </select></label>
        {kind !== "selectivity" && (
          <label>Ready-made analysis
            <select value="" onChange={(e) => preset(e.target.value, kind)}>
              <option value="">choose…</option>
              {(cat?.profiles?.[kind] ?? []).map((p: J, i: number) => <option key={i} value={i}>{kind === "distribution" ? p.label : `${p.table} by ${p.date_field}`}</option>)}
            </select></label>)}
        {kind === "growth" && (<>
          <label>Date field
            <select value={dateField} onChange={(e) => setDateField(e.target.value)}>
              <option value="">choose…</option>
              {(def?.fields ?? []).filter((f: string) => (cat?.date_fields ?? []).includes(f)).map((f: string) => <option key={f}>{f}</option>)}
            </select></label>
          <label>Period
            <select value={period} onChange={(e) => setPeriod(e.target.value)}><option value="month">month</option><option value="year">year</option></select></label>
        </>)}
      </div>
      {kind !== "growth" && def && (
        <fieldset><legend>Fields (up to {cat?.max_fields ?? 3})</legend>
          <div className="row">
            {(def.fields as string[]).map((f) => (
              <label key={f} className="check"><input type="checkbox" checked={fields.includes(f)} onChange={() => toggle(f)} disabled={kind === "distribution" && personal.includes(f)} />
                <span>{f}{personal.includes(f) ? " (personal data)" : ""}</span></label>))}
          </div>
        </fieldset>)}
      <div className="row">
        <button className="primary" disabled={act.busy || !sid || !table || (kind === "growth" ? !dateField : fields.length === 0)} onClick={() => run(kind)}>Analyse</button>
        {kind === "distribution" && <span className="muted small">Personal-data fields are never grouped by value.</span>}
        {kind === "selectivity" && <span className="muted small">For personal-data fields only counts are shown, never values.</span>}
      </div>
    </>
  );

  const meta = (r: J) => (
    <p className="muted small">{r.system} · read {r.rows_read.toLocaleString()} row(s){r.truncated ? " (capped: more rows exist)" : ""}{r.bounded ? " · remote read, bounded by the connection profile" : ""}</p>
  );

  return (
    <>
      <Card title="Data analysis" actions={
        <select aria-label="System to analyse" value={sid} onChange={(e) => setSid(e.target.value)}>
          {app.systems.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
        </select>}>
        <p className="muted">How the data of a source system is distributed, how selective its fields are and how a table grows: the questions a business-process review asks before anything is copied or archived.
          Read-only. Figures are computed from the rows read, nothing is changed, and the audit trail records what was asked, never the answer.</p>
        <ErrorNote error={act.error} />
      </Card>
      <TabList id="an" tabs={TABS} active={tab} onChange={setTab} label="Analysis" />
      <TabPanel id="an" active={tab}>
        {tab === 0 && (
          <Card title="Distribution: which variants hold the volume">
            {chooser("distribution")}
            {res?.kind === "distribution" && (<>
              {meta(res)}
              <div className="grid stats">
                <Stat label="Combinations" value={res.combinations} />
                <Stat label="Cover 50%" value={res.cover["50"] ?? "—"} hint="combinations needed" />
                <Stat label="Cover 80%" value={res.cover["80"] ?? "—"} hint="combinations needed" />
                <Stat label="Cover 95%" value={res.cover["95"] ?? "—"} hint="combinations needed" />
              </div>
              <Bars data={Object.fromEntries(res.top.map((g: J) => [label(g.values), g.rows]))} />
              <DataTable rows={res.top} cols={[...res.fields.map((f: string) => ({ key: f, title: f, render: (g: J) => g.values[f] })),
                { key: "rows", title: "Rows", sortValue: (g: J) => g.rows, render: (g: J) => g.rows.toLocaleString() },
                { key: "share", title: "Share", sortValue: (g: J) => g.share, render: (g: J) => pct(g.share) },
                { key: "cum", title: "Cumulative", sortValue: (g: J) => g.cumulative, render: (g: J) => pct(g.cumulative) }]} />
              {res.other.combinations > 0 && <p className="muted small">{res.other.combinations} more combination(s) with {res.other.rows.toLocaleString()} row(s) are not listed.</p>}
            </>)}
          </Card>)}
        {tab === 1 && (
          <Card title="Selectivity: how unique are the values">
            {chooser("selectivity")}
            {res?.kind === "selectivity" && (<>
              {meta(res)}
              <div className="grid stats">
                <Stat label="Distinct" value={res.distinct.toLocaleString()} />
                <Stat label="Selectivity" value={res.selectivity} hint="distinct ÷ rows" />
                <Stat label="Most repeated" value={res.max_duplicates.toLocaleString()} hint="rows with one value" />
                <Stat label="Verdict" value={<Badge kind={res.verdict === "low selectivity" ? "warn" : "ok"}>{res.verdict}</Badge>} />
              </div>
              {res.values_withheld ? <p className="banner">These fields are personal data: values are withheld, only counts are shown.</p> : (
                <DataTable rows={res.most_repeated} cols={[{ key: "v", title: "Value", render: (g: J) => label(g.values) }, { key: "r", title: "Rows", render: (g: J) => g.rows.toLocaleString() }]} />)}
            </>)}
          </Card>)}
        {tab === 2 && (
          <Card title="Growth: rows per creation period">
            {chooser("growth")}
            {res?.kind === "growth" && (<>
              {meta(res)}
              <div className="grid stats">
                <Stat label="Periods" value={res.periods} />
                <Stat label="Average per period" value={res.average_per_period} />
                <Stat label="Busiest" value={res.busiest ? res.busiest.period : "—"} hint={res.busiest ? `${res.busiest.rows} rows` : ""} />
                <Stat label="Undated rows" value={res.undated} />
              </div>
              <Bars data={Object.fromEntries(res.series.map((s: J) => [s.period, s.rows]))} />
              <DataTable rows={res.series} cols={[{ key: "p", title: "Period", render: (s: J) => s.period }, { key: "r", title: "Rows", render: (s: J) => s.rows.toLocaleString() },
                { key: "c", title: "Cumulative", render: (s: J) => s.cumulative.toLocaleString() }, { key: "ch", title: "Change", render: (s: J) => (s.change == null ? "—" : `${s.change > 0 ? "+" : ""}${Math.round(s.change * 100)}%`) }]} />
            </>)}
          </Card>)}
        {tab === 3 && (
          <Card title="Tables and the business objects that carry them">
            <p className="muted small">Which business object types of this platform contain each table. A table with no object is never copied by a selective refresh.</p>
            <DataTable pageSize={20} rows={tables} cols={[{ key: "t", title: "Table", render: (t: J) => <code>{t.table}</code> }, { key: "d", title: "Description", render: (t: J) => t.description },
              { key: "o", title: "Business objects", render: (t: J) => t.customizing ? <Badge kind="info">customizing: verified, not copied</Badge> : t.objects.length ? t.objects.join(", ") : <Badge kind="warn">none</Badge> },
              { key: "p", title: "Personal fields", render: (t: J) => (t.personal_fields.length ? t.personal_fields.join(", ") : "—") }]} />
          </Card>)}
        {tab === 4 && (
          <Card title="Not available here">
            <p className="muted small">These analyses need runtime statistics or tables the platform does not read. They are listed so their absence is not mistaken for an empty result.</p>
            <ul className="plain">{(cat?.not_available ?? []).map((n: J) => <li key={n.function}><strong>{n.function}</strong><div className="muted small">{n.reason}</div></li>)}</ul>
          </Card>)}
      </TabPanel>
    </>
  );
}

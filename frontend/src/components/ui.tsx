import React, { useState } from "react";

export function Card({ title, children, actions, className }: { title?: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={`card ${className || ""}`}>
      {(title || actions) && <header className="card-h"><h3>{title}</h3><div>{actions}</div></header>}
      <div className="card-b">{children}</div>
    </section>
  );
}

export function Pill({ value }: { value: string | undefined | null }) {
  const v = String(value || "-");
  const cls = /NO_GO|VARIANCES/.test(v) ? "bad" : /PASS|APPROVED|COMPLETED|DONE|IMPLEMENTED|ACCEPTED|LOADED|OPEN$|^GO$|RECONCILED|COMPLETE$|^LOW$|^OK/.test(v) ? "ok" : /FAIL|REJECTED|ERROR|UNSUPPORTED|CONFLICT|HIGH|ABORTED/.test(v) ? "bad" : /WARN|DRAFT|PENDING|RUNNING|SIMULATED|PARTIAL|PLANNED|MEDIUM|PROPOSED|MANUAL|IN_PROGRESS/.test(v) ? "warn" : "neutral";
  return <span className={`pill ${cls}`}>{v}</span>;
}

export function Shield() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-3z" /><path d="M12 8v4" /><path d="M12 15.5h.01" /></svg>;
}

export function Banner({ kind = "info", children, icon = true }: { kind?: "info" | "warn" | "danger"; children: React.ReactNode; icon?: boolean }) {
  return <div className={`banner ${kind}`}>{icon && <Shield />}<div>{children}</div></div>;
}

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "good" | "warn" | "bad" }) {
  return <div className={`stat ${tone || ""}`}><div className="stat-v">{value}</div><div className="stat-l">{label}</div>{sub && <div className="stat-s">{sub}</div>}</div>;
}

export function Tile({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "good" | "warn" | "bad" }) {
  return <div className={`tile stat ${tone || ""}`}><div className="stat-v">{value}</div><div className="stat-l">{label}</div></div>;
}

export function Hero({ title, subtitle }: { title: React.ReactNode; subtitle?: React.ReactNode }) {
  return <div className="hero"><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>;
}

export function Segmented({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return <div className="seg" role="tablist">{options.map((o) => <button key={o.value} type="button" role="tab" aria-selected={value === o.value} className={value === o.value ? "on" : ""} onClick={() => onChange(o.value)}>{o.label}</button>)}</div>;
}

export function List({ items, empty = "Nothing to show", onPick }: { items: { k: React.ReactNode; d?: React.ReactNode; id?: string }[]; empty?: string; onPick?: (id: string) => void }) {
  if (!items.length) return <div className="muted">{empty}</div>;
  return <div className="list">{items.map((it, i) => <div key={it.id || i} className={`item ${onPick ? "row-click" : ""}`} onClick={onPick && it.id ? () => onPick(it.id!) : undefined}><div className="k">{it.k}</div>{it.d && <div className="d">{it.d}</div>}</div>)}</div>;
}

export function Facts({ rows }: { rows: { k: string; v: React.ReactNode; mono?: boolean }[] }) {
  return <dl className="facts">{rows.map((r) => <div key={r.k}><dt>{r.k}</dt><dd className={r.mono ? "mono" : ""}>{r.v}</dd></div>)}</dl>;
}

export function Table({ cols, rows, keyFn, empty = "No data" }: { cols: { k: string; h: string; r?: (row: any) => React.ReactNode; w?: string }[]; rows: any[] | undefined | null; keyFn?: (r: any, i: number) => string; empty?: string }) {
  if (!rows || rows.length === 0) return <div className="muted">{empty}</div>;
  return (
    <div className="tbl-wrap"><table className="tbl">
      <thead><tr>{cols.map((c) => <th key={c.k} style={{ width: c.w }}>{c.h}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={keyFn ? keyFn(r, i) : i}>{cols.map((c) => <td key={c.k}>{c.r ? c.r(r) : String(r[c.k] ?? "")}</td>)}</tr>)}</tbody>
    </table></div>
  );
}

export function KV({ obj, keys }: { obj: Record<string, any> | undefined | null; keys?: string[] }) {
  if (!obj) return null;
  const ks = keys || Object.keys(obj).filter((k) => typeof obj[k] !== "object" || obj[k] === null);
  return <dl className="kv">{ks.map((k) => <React.Fragment key={k}><dt>{k}</dt><dd>{obj[k] === null || obj[k] === undefined ? "-" : typeof obj[k] === "object" ? JSON.stringify(obj[k]) : String(obj[k])}</dd></React.Fragment>)}</dl>;
}

export function Bars({ data, max, unit }: { data: { label: string; value: number; hint?: string }[]; max?: number; unit?: string }) {
  const m = max || Math.max(1, ...data.map((d) => d.value));
  return <div className="bars">{data.map((d) => <div className="bar-row" key={d.label} title={d.hint}><div className="bar-l">{d.label}</div><div className="bar-t"><div className="bar-f" style={{ width: `${Math.max(1, (100 * d.value) / m)}%` }} /></div><div className="bar-v">{d.value.toLocaleString()}{unit || ""}</div></div>)}</div>;
}

export function ErrorBox({ error }: { error: string | null | undefined }) {
  return error ? <div className="banner danger"><Shield /><div>{error}</div></div> : null;
}

export function Pre({ value }: { value: any }) {
  return <pre className="pre">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

export function Tabs({ tabs }: { tabs: { id: string; label: string; content: React.ReactNode }[] }) {
  const [active, setActive] = useState(tabs[0]?.id);
  return <div><div className="tabs">{tabs.map((t) => <button key={t.id} className={active === t.id ? "tab on" : "tab"} onClick={() => setActive(t.id)}>{t.label}</button>)}</div><div>{tabs.find((t) => t.id === active)?.content}</div></div>;
}

export function Select({ value, onChange, options, placeholder }: { value: string | null | undefined; onChange: (v: string) => void; options: { value: string; label: string }[]; placeholder?: string }) {
  return <select value={value || ""} onChange={(e) => onChange(e.target.value)}><option value="">{placeholder || "Select…"}</option>{options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select>;
}

/** True when a registered system runs on the platform's simulators (synthetic record store, simulated add-on or gateway). */
export function isSimulated(s: any): boolean {
  if (!s) return true;
  if (s.connector === "SYNTHETIC") return true;
  const t = s.connector === "RFC" ? s.meta?.rfc?.transport : s.connector === "API" ? s.meta?.api?.transport : "simulated";
  return !t || t === "simulated";
}

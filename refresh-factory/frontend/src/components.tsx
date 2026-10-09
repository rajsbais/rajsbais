import { ReactNode, useMemo, useState } from "react";
import type { J } from "./api";

export function Badge({ kind, children }: { kind?: string; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`}>{children}</span>;
}

const statusKind = (s: string) =>
  ({ pass: "ok", COMPLETED: "ok", RELEASED: "ok", APPROVED: "ok", LOAD: "ok", DONE: "ok", info: "info", SKIP: "info",
     warn: "warn", warning: "warn", QUARANTINE: "warn", HELD: "warn", PENDING_APPROVAL: "warn", RUNNING: "info",
     fail: "bad", blocking: "bad", FAIL: "bad", FAILED: "bad", ROLLED_BACK: "bad" } as Record<string, string>)[s] ?? "";
export const StatusBadge = ({ s }: { s: string }) => <Badge kind={statusKind(s)}>{s}</Badge>;

export const SimBanner = () => (
  <div className="banner" role="note">
    <strong>Simulated SAP.</strong> All data is synthetic and every adapter is a simulation. Nothing here reads or writes a real SAP system.
  </div>
);

export function Card({ title, actions, children }: { title?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && <header><h3>{title}</h3><div className="row">{actions}</div></header>}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return <div className="stat"><div className="v">{value}</div><div className="l">{label}</div>{hint && <div className="h">{hint}</div>}</div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

export interface Col<T> { key: string; title: string; render?: (r: T) => ReactNode; sortValue?: (r: T) => string | number }

type T = J;
export function DataTable({ rows, cols, empty = "Nothing to show", pageSize = 12 }:
  { rows: J[]; cols: Col<J>[]; empty?: string; pageSize?: number }) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [page, setPage] = useState(0);
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    let out = needle ? rows.filter((r) => JSON.stringify(r).toLowerCase().includes(needle)) : rows;
    if (sort) {
      const c = cols.find((c) => c.key === sort.key);
      const val = (r: T) => (c?.sortValue ? c.sortValue(r) : (r as J)[sort.key]);
      out = [...out].sort((a, b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * sort.dir);
    }
    return out;
  }, [rows, q, sort, cols]);
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const cur = Math.min(page, pages - 1);
  return (
    <div className="table-wrap">
      <div className="row between">
        <input className="search" placeholder="Search…" value={q} aria-label="Search table" onChange={(e) => { setQ(e.target.value); setPage(0); }} />
        <span className="muted">{filtered.length} of {rows.length}</span>
      </div>
      <div className="scroll">
        <table>
          <thead><tr>{cols.map((c) => (
            <th key={c.key} onClick={() => setSort((s) => (s?.key === c.key ? { key: c.key, dir: (-s.dir) as 1 | -1 } : { key: c.key, dir: 1 }))}
                aria-sort={sort?.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
              {c.title}{sort?.key === c.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
            </th>))}</tr></thead>
          <tbody>
            {filtered.slice(cur * pageSize, (cur + 1) * pageSize).map((r, i) => (
              <tr key={i}>{cols.map((c) => <td key={c.key}>{c.render ? c.render(r) : String((r as J)[c.key] ?? "")}</td>)}</tr>))}
            {!filtered.length && <tr><td colSpan={cols.length} className="muted">{empty}</td></tr>}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="row between">
          <button disabled={cur === 0} onClick={() => setPage(cur - 1)}>Prev</button>
          <span className="muted">Page {cur + 1} / {pages}</span>
          <button disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>Next</button>
        </div>)}
    </div>
  );
}

export function Bars({ data }: { data: Record<string, number> }) {
  const max = Math.max(1, ...Object.values(data));
  return (
    <div className="bars">
      {Object.entries(data).map(([k, v]) => (
        <div key={k} className="bar-row"><span className="bar-label">{k}</span>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(v / max) * 100}%` }} /></div>
          <span className="bar-val">{v}</span></div>))}
    </div>
  );
}

export function Stepper({ steps, active }: { steps: string[]; active: number }) {
  return (
    <ol className="stepper">
      {steps.map((s, i) => <li key={s} className={i < active ? "done" : i === active ? "cur" : ""}><span>{i + 1}</span>{s}</li>)}
    </ol>
  );
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true); setError(null);
    try { return await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  return { busy, error, run, setError };
}

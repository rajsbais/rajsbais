import { useEffect, useState } from "react";
import { api } from "../api";
import { useApi } from "../hooks";
import { Card, ErrorBox, KV, Pill } from "./ui";

type Opt = { value: string; label: string };

/** Per-system read configuration of the reconciliation (meta.rfc.journal_table / ledger / assets / inventory):
 *  how the totals are read on an S/4HANA system over the add-on. Saved through PUT /systems/{id}/read-config,
 *  which validates it and never touches the transport or destination. */
export function ReadConfig({ systemId, canWrite }: { systemId: string; canWrite: boolean }) {
  const doc = useApi<any>(systemId ? `/systems/${systemId}/read-config` : null, undefined, [systemId]);
  const [form, setForm] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const d = doc.data;
    if (!d) return;
    const e = d.effective;
    setForm({
      journal_table: e.journal_table, ledger: e.ledger,
      assets_source: e.assets.source, assets_area: e.assets.area, assets_apc: (e.assets.apc_movement_categories || []).join(", "), assets_tables: e.assets.apc_tables || [],
      inv_source: e.inventory.source, inv_curtp: e.inventory.currency_type, inv_year: d.configured?.inventory?.period?.year || "", inv_poper: d.configured?.inventory?.period?.poper || "", inv_accounts: (e.inventory.inventory_accounts || []).join(", "),
    });
  }, [doc.data]);
  useEffect(() => { setSaved(null); setErr(null); }, [systemId]);
  if (!doc.data || !form) return doc.error ? <ErrorBox error={doc.error} /> : null;
  const d = doc.data;
  const opts: Record<string, Opt[]> = d.options;
  const set = (k: string, v: any) => setForm({ ...form, [k]: v });
  const payload = () => ({
    journal_table: form.journal_table, ledger: form.ledger,
    assets: { source: form.assets_source, area: form.assets_area, apc_movement_categories: form.assets_apc, apc_tables: form.assets_tables },
    inventory: { source: form.inv_source, currency_type: form.inv_curtp, period: form.inv_year || form.inv_poper ? { year: form.inv_year, poper: form.inv_poper } : null, inventory_accounts: form.inv_accounts },
  });
  const save = async (body: any) => { setBusy(true); setErr(null); setSaved(null); try { const r = await api(`/systems/${systemId}/read-config`, { method: "PUT", body }); doc.setData(r); setSaved(Object.keys(r.configured || {}).length ? "Saved: the next reconciliation of this system reads with this configuration." : "Reset to the defaults."); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const eff = d.effective;
  const summary = { applies: d.applies ? "yes (meta.rfc present)" : "no: this system has no RFC path; API-only targets ignore it", journal: `${eff.journal_table}${eff.journal_table !== "BSEG" ? ` · ledger ${eff.ledger}` : ""}`, assets: `${eff.assets.source} · area ${eff.assets.area}${eff.assets.apc_movement_categories?.length ? ` · APC categories ${eff.assets.apc_movement_categories.join(", ")}` : " · no APC movement categories configured"} · ${(eff.assets.apc_tables || []).join(", ")}`, inventory: `${eff.inventory.source} · currency type ${eff.inventory.currency_type} · period ${eff.inventory.period?.year}/${eff.inventory.period?.poper} (${eff.inventory.period_source})${eff.inventory.inventory_accounts?.length ? ` · accounts ${eff.inventory.inventory_accounts.join(", ")}` : ""}` };
  return (
    <Card title={<>Read configuration <Pill value={d.applies ? (Object.keys(d.configured || {}).length ? "CONFIGURED" : "DEFAULTS") : "NOT_APPLICABLE"} /></>} actions={canWrite && <div className="row"><button disabled={busy || !d.applies} onClick={() => save(payload())}>{busy ? "Saving…" : "Save read configuration"}</button><button className="secondary" disabled={busy || !d.applies} onClick={() => save({})}>Reset to defaults</button></div>}>
      <p className="muted">How the reconciliation reads this system over the read-only add-on: which journal the totals come from and which ledger, how asset acquisition values and inventory values are read on S/4HANA. Nothing here changes the transport or the destination, and no secret passes through. {d.s4hana ? "" : "This is not an S/4HANA system: the asset and inventory chains do not apply; ANLC and MBEW are read directly."}</p>
      <KV obj={summary} />
      <ErrorBox error={err} />
      {saved && <div className="banner info">{saved}</div>}
      <div className="grid2 rc-form" style={{ marginTop: 10 }}>
        <div>
          <h4>Journal</h4>
          <label>Journal table<br /><select value={form.journal_table} onChange={(e) => set("journal_table", e.target.value)}>{opts.journal_table.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
          <label>Ledger (ACDOCA)<br /><input value={form.ledger} maxLength={4} onChange={(e) => set("ledger", e.target.value.toUpperCase())} placeholder="0L" /></label>
          <h4>Asset acquisition values</h4>
          <label>Source<br /><select value={form.assets_source} onChange={(e) => set("assets_source", e.target.value)}>{opts.assets_source.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
          <label>Depreciation area<br /><input value={form.assets_area} maxLength={2} onChange={(e) => set("assets_area", e.target.value)} placeholder="01" /></label>
          <label>APC movement categories (FAA_MOVCAT values of your system, comma-separated)<br /><input value={form.assets_apc} onChange={(e) => set("assets_apc", e.target.value)} placeholder="none assumed: take them from the domain on the system" /></label>
          <div>APC line items in: {(d.options.apc_tables as string[]).map((t) => <label key={t} style={{ display: "inline-block", marginRight: 10 }}><input type="checkbox" checked={form.assets_tables.includes(t)} onChange={(e) => set("assets_tables", e.target.checked ? [...form.assets_tables, t] : form.assets_tables.filter((x: string) => x !== t))} /> {t}</label>)}</div>
        </div>
        <div>
          <h4>Inventory values</h4>
          <label>Source<br /><select value={form.inv_source} onChange={(e) => set("inv_source", e.target.value)}>{opts.inventory_source.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
          <label>Material Ledger currency type<br /><input value={form.inv_curtp} maxLength={2} onChange={(e) => set("inv_curtp", e.target.value)} placeholder="10" /></label>
          <div className="row"><label>Period year<br /><input value={form.inv_year} maxLength={4} onChange={(e) => set("inv_year", e.target.value)} placeholder="calendar year of the read" /></label><label>Posting period<br /><input value={form.inv_poper} maxLength={3} onChange={(e) => set("inv_poper", e.target.value)} placeholder="calendar month" /></label></div>
          <label>Inventory accounts (journal measure, comma-separated)<br /><input value={form.inv_accounts} onChange={(e) => set("inv_accounts", e.target.value)} placeholder="e.g. 13000000, 13100000" /></label>
        </div>
      </div>
      <ul className="muted" style={{ marginTop: 10 }}>{(d.notes || []).map((n: string) => <li key={n}>{n}</li>)}</ul>
    </Card>
  );
}

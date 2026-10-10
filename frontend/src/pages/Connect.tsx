import { useEffect, useState } from "react";
import { api, currentUser } from "../api";
import { useApi, useProjectDetails } from "../hooks";
import { Banner, Card, ErrorBox, Hero, KV, Pill, Pre, Table, isSimulated } from "../components/ui";
import { ReadConfig } from "../components/ReadConfig";

type Kind = "rfc" | "api";

/** One system's connection: the parameters of its destination (no secrets), the transport, and the handshake. */
function ConnectionCard({ system, kind, title, lead, onTested }: { system: any; kind: Kind; title: string; lead: string; onTested: () => void }) {
  const doc = useApi<any>(`/systems/${system.id}/destination`, undefined, [system.id]);
  const [form, setForm] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const [pre, setPre] = useState<any>(null);
  const preflight = async () => {
    setBusy(true); setErr(null); setPre(null);
    try { if (canWrite) await save(); setPre(await api(`/systems/${system.id}/preflight`, { method: "POST" })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const canWrite = !!(currentUser()?.roles || []).some((r: string) => ["architect", "admin"].includes(r));
  useEffect(() => {
    const d = doc.data?.[kind];
    if (!d) return;
    const dest = d.dest || {};
    setForm(kind === "rfc" ? { transport: d.transport || "pyrfc", ashost: dest.ashost || "", sysnr: dest.sysnr || "", client: dest.client || system.client || "", user: dest.user || "", passwd: typeof dest.passwd === "string" && dest.passwd !== "***" ? dest.passwd : "" } : { transport: d.transport || "https", base_url: dest.base_url || "", client: dest.client || system.client || "", user: dest.user || "", passwd: typeof dest.passwd === "string" && dest.passwd !== "***" ? dest.passwd : "", verify_tls: dest.verify_tls !== false });
  }, [doc.data, kind]);
  if (!doc.data) return <Card title={title}>{doc.error ? <ErrorBox error={doc.error} /> : <p className="muted">Loading…</p>}</Card>;
  const d = doc.data[kind];
  const set = (k: string, v: any) => setForm({ ...form, [k]: v });
  const save = async () => {
    if (!form) return;
    const dest: any = kind === "rfc" ? { ashost: form.ashost, sysnr: form.sysnr, client: form.client, user: form.user } : { base_url: form.base_url, client: form.client, user: form.user, verify_tls: form.verify_tls };
    if (form.passwd) dest.passwd = form.passwd.startsWith("env:") ? form.passwd : `env:${form.passwd}`;
    const r = await api(`/systems/${system.id}/destination`, { method: "PUT", params: { kind }, body: { transport: form.transport, dest } });
    doc.setData(r);
    return r;
  };
  const handshake = async () => {
    setBusy(true); setErr(null); setResult(null);
    try {
      if (canWrite) await save();
      const t = await api(`/systems/${system.id}/connector/test`, { method: "POST" });
      setResult(t); onTested();
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  const preflightBlock = pre && <div style={{ marginTop: 14 }}>
    <p className="status-line"><Pill value={pre.summary.ready ? "READY" : "NOT_READY"} /> <span className="muted">{pre.summary.PASS} pass · {pre.summary.WARN} warn · {pre.summary.FAIL} fail · {pre.summary.SKIP} skipped · {pre.duration_ms} ms{pre.simulated ? " · simulated transport" : ""}</span></p>
    <div className="gates">{pre.checks.map((c: any) => <div key={c.id} className={`gate ${c.status === "PASS" ? "passed" : c.status === "FAIL" ? "failed" : c.status === "WARN" ? "blocked" : ""}`}><span>{c.status} · {c.title}<div className="meta">{c.detail}{c.fix ? <><br />fix: {c.fix}</> : null}</div></span></div>)}</div>
    <p className="muted">Next: {pre.next}</p>
  </div>;
  if (!d) return <Card title={title}><p className="muted">{lead}</p><p className="muted">This system has no {kind.toUpperCase()} path: {system.connector === "SYNTHETIC" ? "it is the platform's synthetic landscape (record store)." : `its connector is ${system.connector}.`}</p><div className="row"><button className="secondary" disabled={busy} onClick={preflight}>{busy ? "Checking…" : "Preflight"}</button></div><ErrorBox error={err} />{preflightBlock}</Card>;
  const simulated = form?.transport === "simulated";
  const ok = result?.ok;
  return (
    <Card title={<>{title} <Pill value={system.connector_status} /></>} actions={<span className="muted">{system.sid}/{system.client} · {system.product} {system.release}</span>}>
      <p className="muted" style={{ marginTop: 0 }}>{lead}</p>
      {form && <>
        <label className="field">Transport<select value={form.transport} onChange={(e) => set("transport", e.target.value)} disabled={!canWrite}>{kind === "rfc" ? <><option value="pyrfc">pyrfc: live SAP system through the SAP NW RFC SDK</option><option value="simulated">simulated: the platform's SAP add-on simulator (synthetic data)</option></> : <><option value="https">https: live S/4HANA gateway (released OData APIs)</option><option value="simulated">simulated: the platform's S/4HANA gateway simulator</option></>}</select></label>
        {kind === "rfc" ? <>
          <label className="field">Host<input className="mono" value={form.ashost} onChange={(e) => set("ashost", e.target.value)} placeholder={simulated ? "not used by the simulator" : "vhcalnplci"} disabled={!canWrite} /></label>
          <div className="field inline" style={{ display: "grid" }}><label className="field">System number<input className="mono" value={form.sysnr} maxLength={2} onChange={(e) => set("sysnr", e.target.value)} placeholder="00" disabled={!canWrite} /></label><label className="field">Client<input className="mono" value={form.client} maxLength={3} onChange={(e) => set("client", e.target.value)} placeholder="100" disabled={!canWrite} /></label></div>
          <label className="field">RFC user<input className="mono" value={form.user} onChange={(e) => set("user", e.target.value)} placeholder="SDTF_RFC" disabled={!canWrite} /></label>
        </> : <>
          <label className="field">Host (base URL)<input className="mono" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} placeholder={simulated ? "not used by the simulator" : "https://vhcala4hci.dummy.nodomain:44300"} disabled={!canWrite} /></label>
          <div className="field inline" style={{ display: "grid" }}><label className="field">Client<input className="mono" value={form.client} maxLength={3} onChange={(e) => set("client", e.target.value)} placeholder="100" disabled={!canWrite} /></label><label className="field">API user<input className="mono" value={form.user} onChange={(e) => set("user", e.target.value)} placeholder="SDTF_API" disabled={!canWrite} /></label></div>
          <label className="chk" style={{ marginBottom: 14 }}><input type="checkbox" checked={!!form.verify_tls} onChange={(e) => set("verify_tls", e.target.checked)} disabled={!canWrite} /> verify the TLS certificate (uncheck for a lab system with a self-signed certificate)</label>
        </>}
        <label className="field">Password: environment variable<input className="mono" value={form.passwd} onChange={(e) => set("passwd", e.target.value)} placeholder={`leave empty to use ${d.password_env}`} disabled={!canWrite} /></label>
        <p className="muted">Passwords never enter the platform's database: name the environment variable that holds it on the machine running the platform, or set <code>{d.password_env}</code>. Resolved destination: <code>{JSON.stringify(d.resolved)}</code>.</p>
        <div className="row" style={{ marginBottom: 8 }}><button className="secondary" disabled={busy} onClick={preflight}>{busy ? "Checking…" : "Preflight"}</button><span className="muted">the prerequisite checklist with a fix per failure: SDK, destination, secret, DNS, port, logon, add-on, handshake, authorisations</span></div>
        <button className="block" disabled={busy} onClick={handshake}>{busy ? "Testing…" : kind === "rfc" ? "Test ECC handshake" : "Test S/4HANA handshake"}</button>
        {!canWrite && <p className="muted">Architects save the destination; other roles can only run the handshake with the stored one.</p>}
      </>}
      <ErrorBox error={err} />
      {preflightBlock}
      {result && <div style={{ marginTop: 14 }}>
        <p className="status-line"><Pill value={ok ? "HANDSHAKE_OK" : "HANDSHAKE_FAILED"} /> <span className="muted">{result.transport} · {result.duration_ms} ms</span></p>
        <KV obj={ok ? (result.connector === "API" ? { csrf_token: result.csrf_token ? "fetched" : "none", services: (result.services || []).join(", "), journal_entry_numbering: result.numbering?.JournalEntry, company_code: result.probe?.company_code } : { snapshot: result.snapshot, valid_until: result.valid_until, table: result.table?.table, rows_in_table: result.table?.rows, sample_rows: result.sample_rows, checksum_verified: result.checksum_verified ? "yes" : "no", aggregate_module: result.aggregate?.available ? "available" : `missing (${result.aggregate?.error || ""})` }) : { error: result.error, detail: result.detail }} />
        {result.rfc_readback && <KV obj={result.rfc_readback.ok ? { rfc_readback: `ok via ${result.rfc_readback.transport}`, rfc_sample_rows: result.rfc_readback.sample_rows, rfc_aggregate: result.rfc_readback.aggregate?.available ? "available" : "missing" } : { rfc_readback: `failed: ${result.rfc_readback.error}`, rfc_detail: result.rfc_readback.detail }} />}
        <p className="muted">{result.transport === "SIMULATED_ADDON" ? "Simulated SAP add-on: the contract ran on synthetic data; no SAP system is involved." : result.transport === "SIMULATED_S4" ? "Simulated S/4HANA gateway: released APIs over the target record store; no SAP system is involved." : result.connector === "API" ? "Live S/4HANA via HTTPS: CSRF token fetched; nothing was written." : "Live SAP system via pyrfc: snapshot opened, T001 metadata read and one 5-row package verified against its checksum."}</p>
      </div>}
    </Card>
  );
}

export default function Connect() {
  const { project, source, target, reload } = useProjectDetails();
  const [sys, setSys] = useState<string | null>(null);
  const sysId = sys || source?.id;
  const system = project?.systems?.find((s: any) => s.id === sysId);
  const disc = useApi<any>(sysId ? `/systems/${sysId}/discovery` : null, undefined, [sysId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [meta, setMeta] = useState<any>(null);
  const discover = async () => { setBusy(true); setErr(null); try { await api(`/systems/${sysId}/discover`, { method: "POST" }); disc.reload(); reload(); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  const checkMetadata = async () => { setBusy(true); setErr(null); setMeta(null); try { setMeta(await api(`/systems/${sysId}/connector/metadata-check`, { method: "POST" })); } catch (e: any) { setErr(e.message); } finally { setBusy(false); } };
  if (!project) return <Banner>Select or create a project first.</Banner>;
  const allSim = (project.systems || []).every(isSimulated);
  return (
    <div>
      <Hero title="System connections" subtitle={allSim ? "RFC to the simulated ECC add-on and OData to the simulated S/4HANA gateway. A customer host is not reached from this build." : "RFC to the source through the read-only add-on and OData to the target's released APIs. Reads only until a load you start."} />
      {source && <ConnectionCard system={source} kind="rfc" title={`Source ${source.product}`} lead="ABAP extraction agents via RFC: Z_SDTF_OPEN_SNAPSHOT, Z_SDTF_READ_PACKAGE, Z_SDTF_TABLE_METADATA, Z_SDTF_AGGREGATE, Z_SDTF_CDC_POLL (read-only function group, sap-abap/)." onTested={reload} />}
      {target && <ConnectionCard system={target} kind="api" title={`Target ${target.product}`} lead="Load agent via the released OData APIs (business partner, product, sales order, purchase order, journal entry) and the migration cockpit packages." onTested={reload} />}
      {target && target.meta?.rfc && <ConnectionCard system={target} kind="rfc" title={`Target ${target.product}: read-back over RFC`} lead="The read-only add-on on the target serves the reconciliation what the APIs cannot (company codes, valuation areas, asset values, aggregate-only mode)." onTested={reload} />}
      <Card title="Discovery and read configuration" actions={<><select value={sysId || ""} onChange={(e) => setSys(e.target.value)}>{project.systems.map((x: any) => <option key={x.id} value={x.id}>{x.role} {x.sid}/{x.client} ({x.product} {x.release})</option>)}</select> <button disabled={busy || !sysId} onClick={discover}>{busy ? "Discovering…" : system?.connector === "RFC" ? "Run discovery through the add-on" : "Run discovery"}</button>{system?.connector === "API" && <button className="secondary" disabled={busy} onClick={checkMetadata}>Metadata check</button>}</>}>
        <Table cols={[{ k: "role", h: "Role" }, { k: "sid", h: "SID" }, { k: "client", h: "Client" }, { k: "product", h: "Product" }, { k: "release", h: "Release" }, { k: "connector", h: "Connector" }, { k: "connector_status", h: "Status", r: (r) => <Pill value={r.connector_status} /> }, { k: "logical_system", h: "Logical system" }]} rows={project.systems} />
        <ErrorBox error={err} />
        {disc.data?.summary && <p className="muted">Discovery snapshot of {system?.sid}: {disc.data.summary.tables.count} tables ({disc.data.summary.tables.custom} custom), {disc.data.summary.org_units.COMPANY_CODE} company codes, {disc.data.summary.org_units.PLANT} plants, complexity {disc.data.summary.complexity.band}; read {disc.data.summary.read?.path === "rfc" ? `through the add-on (${disc.data.summary.read.transport}, ${disc.data.summary.read.rfc_calls} calls, ${disc.data.summary.read.sample} instances sampled per object type)` : "from the platform's record store"}{Object.keys(disc.data.summary.read?.unreadable || {}).length ? ` · not readable: ${Object.keys(disc.data.summary.read.unreadable).join(", ")}` : ""}. Browse it under Landscape.</p>}
        {disc.error && <p className="muted">No discovery snapshot yet for {system?.sid}.</p>}
        {meta && <Card title={`API metadata check (${meta.transport}): ${Object.entries(meta.summary || {}).filter(([, n]: any) => n).map(([k, n]) => `${n} ${k}`).join(", ")}`}>
          <Table cols={[{ k: "service", h: "Service" }, { k: "activated", h: "Activated", r: (x) => x.activated === null || x.activated === undefined ? "?" : x.activated ? "yes" : "no" }, { k: "verdict", h: "Verdict", r: (x) => <Pill value={x.verdict === "VERIFIED" ? "PASS" : x.verdict === "DEVIATIONS" ? "WARN" : x.verdict} /> }, { k: "entity_sets", h: "Entity sets", r: (x) => (x.entity_sets || []).map((e: any) => `${e.entity_set}: ${e.found ? `${e.properties_found.length}/${e.properties.length}` : "missing"}`).join(" · ") || x.error || "" }, { k: "missing", h: "Missing properties", r: (x) => (x.entity_sets || []).flatMap((e: any) => (e.properties_missing || []).map((p: string) => `${p}${(e.suggestions?.[p] || []).length ? ` → ${e.suggestions[p].join("/")}` : ""}`)).join(", ") }]} rows={meta.services || []} />
          <Pre value={meta.markdown} />
        </Card>}
      </Card>
      {sysId && (system?.connector === "RFC" || system?.connector === "API") && <ReadConfig systemId={sysId} canWrite={!!(currentUser()?.roles || []).some((r: string) => ["architect", "admin"].includes(r))} />}
    </div>
  );
}

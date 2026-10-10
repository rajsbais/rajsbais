import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { currentUser, login } from "./api";
import { CALLBACK_PATH, beginOidcLogin, completeOidcLogin, isAuthenticated, oidcConfig, scheduleRefresh, signOut } from "./auth/session";
import type { OidcPublicConfig } from "./auth/pkce";
import { useApi, useProject, useProjectDetails } from "./hooks";
import { Banner, Card, ErrorBox, Hero, isSimulated } from "./components/ui";
import { groupFor, NAV, pageFor } from "./lib";
import Dashboard from "./pages/Dashboard";
import Landscape from "./pages/Landscape";
import Analyzer from "./pages/Analyzer";
import OrgStructure from "./pages/OrgStructure";
import Catalog from "./pages/Catalog";
import GraphExplorer from "./pages/GraphExplorer";
import ScopeDesigner from "./pages/ScopeDesigner";
import CarveoutStudio from "./pages/CarveoutStudio";
import BluefieldStudio from "./pages/BluefieldStudio";
import RulesWorkbench from "./pages/RulesWorkbench";
import DataQuality from "./pages/DataQuality";
import RunsMonitor from "./pages/RunsMonitor";
import DeltaMonitor from "./pages/DeltaMonitor";
import Reconciliation from "./pages/Reconciliation";
import Cutover from "./pages/Cutover";
import Copilot from "./pages/Copilot";
import Compliance from "./pages/Compliance";
import Portfolio from "./pages/Portfolio";
import Merger from "./pages/Merger";
import Audit from "./pages/Audit";
import Connect from "./pages/Connect";
import Extract from "./pages/Extract";

const ICONS: Record<string, JSX.Element> = {
  grid: <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="8" height="8" rx="1.5" /><rect x="13" y="3" width="8" height="8" rx="1.5" /><rect x="3" y="13" width="8" height="8" rx="1.5" /><rect x="13" y="13" width="8" height="8" rx="1.5" /></svg>,
  building: <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="1.5" /><path d="M9 7h2M13 7h2M9 11h2M13 11h2M9 15h2M13 15h2M10 21v-3h4v3" /></svg>,
  factory: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21V9l6 4V9l6 4V9l6 4v8H3z" /><path d="M7 17h2M11 17h2M15 17h2" /></svg>,
  graph: <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M7.8 7.6l3 7.8M16.2 7.6l-3 7.8M8.5 6h7" /></svg>,
  play: <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M10 8.5v7l5.5-3.5z" /></svg>,
  clipboard: <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="4" width="14" height="17" rx="2" /><path d="M9 4.5V3h6v1.5M9 12l2 2 4-4" /></svg>,
  "clipboard-check": <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="4" width="14" height="17" rx="2" /><path d="M9 4.5V3h6v1.5M9 10h6M9 14h6M9 18h3" /></svg>,
  plug: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3v5M15 3v5M7 8h10v4a5 5 0 0 1-10 0V8zM12 17v4" /></svg>,
  extract: <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="6" r="2.5" /><circle cx="6" cy="18" r="2.5" /><circle cx="18" cy="12" r="2.5" /><path d="M8.2 7.2l7.6 3.6M8.2 16.8l7.6-3.6" /></svg>,
  scale: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v18M4 7h16M6 7l-3 7h6l-3-7zM18 7l-3 7h6l-3-7zM8 21h8" /></svg>,
  clock: <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="13" r="8" /><path d="M12 9v4l3 2M9 3h6" /></svg>,
  bank: <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 10l9-6 9 6H3zM5 10v8M9 10v8M15 10v8M19 10v8M3 20h18" /></svg>,
};

function Login({ onDone, returnTo }: { onDone: () => void; returnTo: string }) {
  const [u, setU] = useState("architect");
  const [p, setP] = useState("architect");
  const [err, setErr] = useState<string | null>(null);
  const [cfg, setCfg] = useState<OidcPublicConfig | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { oidcConfig().then(setCfg); }, []);
  const sso = !!cfg?.enabled;
  const dev = cfg ? cfg.dev_login !== false : true;
  const issuerHost = (() => { try { return cfg?.issuer ? new URL(cfg.issuer).host : ""; } catch { return cfg?.issuer || ""; } })();
  return (
    <div className="login"><p className="eyebrow" style={{ color: "var(--muted)" }}>Transform Factory</p><Card title="Sign in to SDTF">
      {!cfg && <p className="muted">Loading sign-in options…</p>}
      {sso && (
        <div className="sso">
          <button type="button" disabled={busy} onClick={() => { setBusy(true); setErr(null); beginOidcLogin(returnTo).catch((x) => { setErr(x.message); setBusy(false); }); }}>
            {busy ? "Redirecting to your identity provider…" : `Sign in with single sign-on${issuerHost ? ` (${issuerHost})` : ""}`}
          </button>
          <p className="muted">OpenID Connect authorization code flow with PKCE. Your password never reaches SDTF; roles come from your directory groups.</p>
          {cfg?.error && <ErrorBox error={`Identity provider unreachable: ${cfg.error}`} />}
        </div>
      )}
      {sso && dev && <div className="divider"><span>or</span></div>}
      {dev && (
        <details open={!sso}>
          <summary className="muted">Development users: admin, architect, approver, operator, auditor, viewer (password = username).</summary>
          <form className="form" onSubmit={(e) => { e.preventDefault(); login(u, p).then(onDone).catch((x) => setErr(x.message)); }}>
            <label>Username<input value={u} onChange={(e) => setU(e.target.value)} /></label>
            <label>Password<input type="password" value={p} onChange={(e) => setP(e.target.value)} /></label>
            <button type="submit">Sign in</button>
          </form>
        </details>
      )}
      {cfg && !sso && !dev && <ErrorBox error="No sign-in method is enabled: configure SDTF_OIDC_* for single sign-on or SDTF_DEV_USERS=1 for development users." />}
      <ErrorBox error={err} />
    </Card></div>
  );
}

/** /auth/callback: the identity provider sends the browser back here with ?code&state. */
function OidcCallback({ onDone }: { onDone: (path: string) => void }) {
  const [err, setErr] = useState<string | null>(null);
  const loc = useLocation();
  useEffect(() => {
    completeOidcLogin(loc.search).then(onDone).catch((x) => setErr(x.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="login"><Card title="Completing sign-in">
      {!err && <p className="muted">Verifying the authorization code with the identity provider…</p>}
      {err && <>
        <ErrorBox error={`Sign-in failed: ${err}`} />
        <button type="button" onClick={() => onDone("/")}>Back to sign-in</button>
      </>}
    </Card></div>
  );
}

function ProjectPicker() {
  const { projectId, setProjectId } = useProject();
  const { data } = useApi<any[]>("/projects");
  useEffect(() => { if (data && data.length && !data.find((p) => p.id === projectId)) setProjectId(data[0].id); }, [data]);
  return <select aria-label="Project" value={projectId || ""} onChange={(e) => setProjectId(e.target.value)}>{(data || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}{(!data || !data.length) && <option value="">No projects yet - create one in Portfolio</option>}</select>;
}

/** The honesty banner: what this project's systems really are. */
function HonestyBanner({ project }: { project: any }) {
  const systems: any[] = project?.systems || [];
  const live = systems.filter((s) => !isSimulated(s));
  if (!systems.length || !live.length) return <Banner kind="warn">Synthetic ECC landscape and simulated S/4HANA gateway only. No customer host is reached from this build. Extraction, rules, CDC, finance and cutover calculate against that fixture.</Banner>;
  return <Banner kind="info">Connected systems: {live.map((s) => `${s.sid}/${s.client} (${s.role.toLowerCase()}, ${s.connector})`).join(", ")}. Reads go through the read-only add-on and the released APIs; nothing is written to SAP until a load you start. {live.length < systems.length ? "The other systems of this project are simulated." : ""}</Banner>;
}

export default function App() {
  const [authed, setAuthed] = useState(isAuthenticated());
  const loc = useLocation();
  const nav = useNavigate();
  useEffect(() => { setAuthed(isAuthenticated()); }, [loc]);
  useEffect(() => {
    const h = () => setAuthed(isAuthenticated());
    window.addEventListener("sdtf.auth", h);
    scheduleRefresh();
    return () => window.removeEventListener("sdtf.auth", h);
  }, []);
  if (loc.pathname === CALLBACK_PATH) return <OidcCallback onDone={(path) => { setAuthed(isAuthenticated()); nav(path, { replace: true }); }} />;
  if (!authed) return <Login onDone={() => setAuthed(true)} returnTo={loc.pathname + loc.search} />;
  return <Shell />;
}

function Shell() {
  const loc = useLocation();
  const user = currentUser();
  const { project } = useProjectDetails();
  const group = groupFor(loc.pathname);
  const page = pageFor(loc.pathname);
  useEffect(() => { document.title = `${page?.title || "SDTF"} · Transform Factory`; }, [page?.title]);
  return (
    <div className="shell">
      <header className="hdr">
        <div className="hdr-top">
          <div><p className="eyebrow">Transform Factory</p><h1>{project?.name || "SDTF"}</h1></div>
        </div>
        <nav className="primary-nav" aria-label="Primary">{NAV.map((g) => <NavLink key={g.id} to={g.items[0].to} end={g.items[0].to === "/"} className={() => (group.id === g.id ? "active" : "")}>{ICONS[g.icon]}<span>{g.label}</span></NavLink>)}</nav>
        <div className="who"><ProjectPicker /><span title={user?.method === "oidc" ? "Signed in through single sign-on" : "Development user"}>{user?.display_name} ({user?.roles.join(", ")}){user?.method === "oidc" ? " · SSO" : ""}</span><button type="button" onClick={() => { signOut().then(() => window.dispatchEvent(new Event("sdtf.auth"))); }}>Sign out</button></div>
      </header>
      <main className="main">
        {group.items.length > 1 && <nav className="sub-nav" aria-label={group.label}>{group.items.map((i) => <NavLink key={i.to} to={i.to} end className={({ isActive }) => (isActive ? "active" : "")}>{i.label}</NavLink>)}</nav>}
        <HonestyBanner project={project} />
        {page && page.subtitle && <Hero title={page.title} subtitle={page.subtitle} />}
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/landscape" element={<Landscape />} />
          <Route path="/analyzer" element={<Analyzer />} />
          <Route path="/org" element={<OrgStructure />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/graph" element={<GraphExplorer />} />
          <Route path="/scope" element={<ScopeDesigner />} />
          <Route path="/carveout" element={<CarveoutStudio />} />
          <Route path="/bluefield" element={<BluefieldStudio />} />
          <Route path="/merger" element={<Merger />} />
          <Route path="/rules" element={<RulesWorkbench />} />
          <Route path="/quality" element={<DataQuality />} />
          <Route path="/runs" element={<RunsMonitor />} />
          <Route path="/delta" element={<DeltaMonitor />} />
          <Route path="/reconciliation" element={<Reconciliation />} />
          <Route path="/cutover" element={<Cutover />} />
          <Route path="/copilot" element={<Copilot />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/connect" element={<Connect />} />
          <Route path="/extract" element={<Extract />} />
          <Route path="/compliance" element={<Compliance />} />
        </Routes>
      </main>
    </div>
  );
}

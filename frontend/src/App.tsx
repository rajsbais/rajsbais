import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { currentUser, login } from "./api";
import { CALLBACK_PATH, beginOidcLogin, completeOidcLogin, isAuthenticated, oidcConfig, scheduleRefresh, signOut } from "./auth/session";
import type { OidcPublicConfig } from "./auth/pkce";
import { useApi, useProject } from "./hooks";
import { Card, ErrorBox } from "./components/ui";
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

const NAV: { group: string; items: { to: string; label: string }[] }[] = [
  { group: "Overview", items: [{ to: "/", label: "Executive Dashboard" }, { to: "/portfolio", label: "Migration Factory Portfolio" }] },
  { group: "Discover", items: [{ to: "/landscape", label: "SAP Landscape Explorer" }, { to: "/analyzer", label: "Enterprise Analyzer" }, { to: "/org", label: "Organizational Structure" }, { to: "/catalog", label: "Business Object Catalog" }, { to: "/graph", label: "Dependency Graph Explorer" }] },
  { group: "Design", items: [{ to: "/scope", label: "Selective Scope Designer" }, { to: "/carveout", label: "Carve-out Studio" }, { to: "/bluefield", label: "Bluefield Transformation Studio" }, { to: "/merger", label: "Merger & Consolidation" }, { to: "/rules", label: "Mapping & Rules Workbench" }] },
  { group: "Execute", items: [{ to: "/quality", label: "Data Quality Dashboard" }, { to: "/runs", label: "Extraction & Load Monitor" }, { to: "/delta", label: "Delta Synchronization Monitor" }, { to: "/reconciliation", label: "Reconciliation Center" }, { to: "/cutover", label: "Cutover Command Center" }] },
  { group: "Govern", items: [{ to: "/copilot", label: "AI Transformation Copilot" }, { to: "/compliance", label: "Compliance & Evidence Center" }] },
];

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
    <div className="login"><Card title="Sign in to SDTF">
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
  return <select value={projectId || ""} onChange={(e) => setProjectId(e.target.value)}>{(data || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}{(!data || !data.length) && <option value="">No projects yet - create one in Portfolio</option>}</select>;
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
  const user = currentUser();
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">SDTF<small>SAP Selective Data Transformation Factory</small></div>
        <nav className="nav">{NAV.map((g) => <div key={g.group}><div className="group">{g.group}</div>{g.items.map((i) => <NavLink key={i.to} to={i.to} end={i.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>{i.label}</NavLink>)}</div>)}</nav>
      </aside>
      <main className="main">
        <div className="topbar">
          <h1>{NAV.flatMap((g) => g.items).find((i) => i.to === loc.pathname)?.label || "SDTF"}</h1>
          <ProjectPicker />
          <span className="muted" title={user?.method === "oidc" ? "Signed in through single sign-on" : "Development user"}>{user?.display_name} ({user?.roles.join(", ")}){user?.method === "oidc" ? " · SSO" : ""}</span>
          <button className="secondary" onClick={() => { signOut().then(() => setAuthed(false)); }}>Sign out</button>
        </div>
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
          <Route path="/compliance" element={<Compliance />} />
        </Routes>
      </main>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { api, getToken, getUser, setToken, setUser, store, J } from "./api";
import { AppCtx } from "./ctx";
import { ErrorNote, SimBanner } from "./components";
import ControlTower from "./views/ControlTower";
import { FullRefresh, Readiness } from "./views/Basis";
import PostCopy from "./views/PostCopy";
import TestCatalog from "./views/TestCatalog";
import LeanClient from "./views/LeanClient";
import Landscape from "./views/Landscape";
import Designer from "./views/Designer";
import Delta from "./views/Delta";
import Dependencies from "./views/Dependencies";
import Masking from "./views/Masking";
import Conflicts from "./views/Conflicts";
import Execution from "./views/Execution";
import Reconciliation from "./views/Reconciliation";
import Compliance from "./views/Compliance";
import Agents from "./views/Agents";
import Orchestration from "./views/Orchestration";

const GROUPS: { group: string; views: { id: string; label: string; el: () => JSX.Element; perm?: string }[] }[] = [
  { group: "Overview", views: [
    { id: "dashboard", label: "Control tower", el: ControlTower }, { id: "landscape", label: "Landscape", el: Landscape },
    { id: "readiness", label: "Readiness", el: Readiness }] },
  { group: "Refresh", views: [
    { id: "designer", label: "Selective designer", el: Designer }, { id: "delta", label: "Delta refresh", el: Delta },
    { id: "dependencies", label: "Dependencies", el: Dependencies },
    { id: "conflicts", label: "Conflicts", el: Conflicts }, { id: "masking", label: "Masking", el: Masking },
    { id: "execution", label: "Execution", el: Execution }, { id: "reconciliation", label: "Reconciliation", el: Reconciliation }] },
  { group: "Basis", views: [{ id: "lean", label: "Lean client", el: LeanClient }, { id: "fullrefresh", label: "Full refresh", el: FullRefresh }, { id: "postcopy", label: "Post-copy", el: PostCopy }, { id: "orchestration", label: "Orchestration", el: Orchestration }] },
  { group: "Data", views: [
    { id: "catalog", label: "Test catalog", el: TestCatalog }, { id: "compliance", label: "Audit", el: Compliance, perm: "audit:read" },
    { id: "agents", label: "AI agents", el: Agents }] },
];
const VIEWS = GROUPS.flatMap((g) => g.views);

export default function App() {
  const [view, setView] = useState(store.get("rf.view") || "dashboard");
  const [userId, setUserId] = useState(store.get("rf.user") || "alice.basis");
  const [me, setMe] = useState<J>(null);
  const [users, setUsers] = useState<J[]>([]);
  const [systems, setSystems] = useState<J[]>([]);
  const [projects, setProjects] = useState<J[]>([]);
  const [projectId, setProjectId] = useState<string | null>(store.get("rf.project"));
  const [runId, setRunId] = useState<string | null>(store.get("rf.run"));
  const [error, setError] = useState<string | null>(null);
  const [nav, setNav] = useState(false);
  const [authCfg, setAuthCfg] = useState<J>(null);
  const [tokenIn, setTokenIn] = useState("");
  useEffect(() => { fetch("/api/auth/config").then((r) => r.json()).then(setAuthCfg).catch(() => setAuthCfg({ mode: "demo" })); }, []);
  const oidc = authCfg?.mode === "oidc";

  setUser(userId);
  const reload = useCallback(async () => {
    try {
      setError(null);
      const [m, u, s, p] = await Promise.all([api.get("/api/me"), api.get("/api/users"), api.get("/api/systems"), api.get("/api/projects")]);
      setMe(m); setUsers(u); setSystems(s); setProjects(p);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { setUser(userId); store.set("rf.user", userId); void reload(); }, [userId, reload]);

  const go = (v: string) => { setView(v); store.set("rf.view", v); setNav(false); };
  const project = projects.find((p) => p.id === projectId) ?? null;
  const ctx = {
    me, users, systems, projects, project, runId, reload, go,
    selectProject: (id: string | null) => { setProjectId(id); store.set("rf.project", id ?? ""); },
    setRunId: (id: string | null) => { setRunId(id); store.set("rf.run", id ?? ""); },
    can: (perm: string) => !!me?.permissions?.includes(perm),
  };
  const current = VIEWS.find((v) => v.id === view) ?? VIEWS[0];
  const View = current.el;
  useEffect(() => { document.title = `${current.label} · Keystone`; }, [current.label]);

  return (
    <AppCtx.Provider value={ctx}>
      <a className="skip" href="#main">Skip to main content</a>
      <div className="shell">
        <aside className={nav ? "side open" : "side"}>
          <nav aria-label="Main">
            {GROUPS.map((g) => (
              <div key={g.group} className="nav-group"><div className="eyebrow">{g.group}</div>
                {g.views.map((v) => (
                  <button key={v.id} className={v.id === current.id ? "nav active" : "nav"} aria-current={v.id === current.id ? "page" : undefined} onClick={() => go(v.id)}
                          disabled={!!v.perm && !!me && !ctx.can(v.perm)} title={v.perm && !ctx.can(v.perm) ? `requires ${v.perm}` : ""}>
                    {v.label}
                  </button>))}
              </div>))}
          </nav>
          <div className="who">
            {oidc ? (
              me && getToken() ? (
                <>
                  <div>Signed in as <strong>{me.name}</strong></div>
                  <div className="muted small">{me.roles?.join(", ") || "no roles"}{me.kind !== "human" ? ` · ${me.kind}` : ""}</div>
                  <button onClick={() => { setToken(null); setMe(null); setTokenIn(""); void reload(); }}>Sign out</button>
                </>) : (
                <>
                  <label htmlFor="token">Bearer token (OIDC)</label>
                  <input id="token" type="password" autoComplete="off" value={tokenIn} onChange={(e) => setTokenIn(e.target.value)} />
                  <button disabled={!tokenIn} onClick={() => { setToken(tokenIn.trim()); setTokenIn(""); void reload(); }}>Sign in</button>
                  <div className="muted small">No browser login flow yet: paste a short-lived token from your identity provider.</div>
                </>)
            ) : (
              <>
                <label htmlFor="user">Signed in as (demo auth)</label>
                <select id="user" value={getUser()} onChange={(e) => setUserId(e.target.value)}>
                  {users.map((u) => <option key={u.id} value={u.id}>{u.name}{u.kind === "agent" ? " [AI agent]" : ""}</option>)}
                </select>
                <div className="muted small">{me?.roles?.join(", ")}</div>
              </>)}
            {me?.scope && <div className="muted small" data-testid="scope">Scope: {me.scope.company_codes ? `company ${me.scope.company_codes.join(", ")}` : "all companies"} · {me.scope.systems ? me.scope.systems.join(", ") : "all systems"}</div>}
          </div>
        </aside>
        <main id="main" tabIndex={-1}>
          <h1 className="sr-only">{current.label}</h1>
          <header className="top">
            <button className="menu" onClick={() => setNav(!nav)} aria-label="Toggle navigation">☰</button>
            <div className="brandbar"><div className="eyebrow brandname">Keystone</div><div>SAP refresh control tower · synthetic {project ? (project.source.family === "S4" ? "S/4HANA" : "ECC") : "ECC + S/4HANA"}</div></div>
            <div className="route">
              <div className="mono">{project ? `${project.source.role}/${project.source.client} → ${project.target.sid}/${project.target.client}` : "no project"}</div>
              <div className="muted">{project ? project.status.replace(/_/g, " ").toLowerCase() : "idle"}</div>
              <select value={projectId ?? ""} onChange={(e) => ctx.selectProject(e.target.value || null)} aria-label="Active refresh project">
                <option value="">— no project —</option>
                {projects.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.status}</option>)}
              </select>
            </div>
          </header>
          <div className="content">
            <SimBanner />
            <ErrorNote error={error} />
            <View />
          </div>
        </main>
      </div>
    </AppCtx.Provider>
  );
}

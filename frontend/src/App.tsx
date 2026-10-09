import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { currentUser, getToken, login, logout } from "./api";
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

const NAV: { group: string; items: { to: string; label: string }[] }[] = [
  { group: "Overview", items: [{ to: "/", label: "Executive Dashboard" }, { to: "/portfolio", label: "Migration Factory Portfolio" }] },
  { group: "Discover", items: [{ to: "/landscape", label: "SAP Landscape Explorer" }, { to: "/analyzer", label: "Enterprise Analyzer" }, { to: "/org", label: "Organizational Structure" }, { to: "/catalog", label: "Business Object Catalog" }, { to: "/graph", label: "Dependency Graph Explorer" }] },
  { group: "Design", items: [{ to: "/scope", label: "Selective Scope Designer" }, { to: "/carveout", label: "Carve-out Studio" }, { to: "/bluefield", label: "Bluefield Transformation Studio" }, { to: "/rules", label: "Mapping & Rules Workbench" }] },
  { group: "Execute", items: [{ to: "/quality", label: "Data Quality Dashboard" }, { to: "/runs", label: "Extraction & Load Monitor" }, { to: "/delta", label: "Delta Synchronization Monitor" }, { to: "/reconciliation", label: "Reconciliation Center" }, { to: "/cutover", label: "Cutover Command Center" }] },
  { group: "Govern", items: [{ to: "/copilot", label: "AI Transformation Copilot" }, { to: "/compliance", label: "Compliance & Evidence Center" }] },
];

function Login({ onDone }: { onDone: () => void }) {
  const [u, setU] = useState("architect");
  const [p, setP] = useState("architect");
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="login"><Card title="Sign in to SDTF">
      <p className="muted">Development users: admin, architect, approver, operator, auditor, viewer (password = username).</p>
      <form className="form" onSubmit={(e) => { e.preventDefault(); login(u, p).then(onDone).catch((x) => setErr(x.message)); }}>
        <label>Username<input value={u} onChange={(e) => setU(e.target.value)} /></label>
        <label>Password<input type="password" value={p} onChange={(e) => setP(e.target.value)} /></label>
        <button type="submit">Sign in</button>
      </form>
      <ErrorBox error={err} />
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
  const [authed, setAuthed] = useState(!!getToken());
  const loc = useLocation();
  useEffect(() => { setAuthed(!!getToken()); }, [loc]);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
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
          <span className="muted">{user?.display_name} ({user?.roles.join(", ")})</span>
          <button className="secondary" onClick={() => { logout(); setAuthed(false); }}>Sign out</button>
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

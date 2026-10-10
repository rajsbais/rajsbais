import { useApp } from "../ctx";

interface Tile { id?: string; label: string; hint: string; perm?: string }
interface Group { title: string; tiles: Tile[] }

// A launcher in the manner of a classic SAP solution menu: every tool of the platform, grouped by what it is for. A tile without an `id` is a
// function the platform does not have yet; it is shown disabled and says so instead of being left out.
const LEFT: Group[] = [
  { title: "Client construct", tiles: [
    { id: "lean", label: "Lean client", hint: "Build a small purpose-made client" },
    { id: "fullrefresh", label: "Full refresh", hint: "Copy a whole system or client" },
    { id: "postcopy", label: "Post-copy", hint: "Clean-up steps after a copy, with a gate" }] },
  { title: "Repository and configuration", tiles: [
    { id: "readiness", label: "Readiness", hint: "Is the target ready for a refresh?" },
    { id: "conflicts", label: "Conflicts", hint: "Customizing gaps and clashing objects" },
    { label: "System build plus", hint: "Not built yet" }] },
  { title: "Waves", tiles: [{ id: "orchestration", label: "Orchestration", hint: "Several refreshes in a planned order" }] },
];
const MIDDLE: Group[] = [
  { title: "Selective data", tiles: [
    { id: "designer", label: "Selective designer", hint: "Choose objects, scope and plan" },
    { id: "delta", label: "Delta refresh", hint: "Move only what changed" },
    { id: "analysis", label: "Data analysis", hint: "Distribution, selectivity and growth of tables" },
    { id: "dependencies", label: "Dependencies", hint: "What a business object needs" },
    { id: "catalog", label: "Test catalog", hint: "Datasets and test data on request" },
    { id: "execution", label: "Execution", hint: "Run, resume, roll back" },
    { id: "reconciliation", label: "Reconciliation", hint: "Prove the target is right" }] },
  { title: "Data transform", tiles: [
    { id: "masking", label: "Masking", hint: "Protect personal and sensitive data" },
    { id: "compliance", label: "Audit and evidence", hint: "Signed trail and evidence packs", perm: "audit:read" }] },
  { title: "Data discard", tiles: [{ label: "Delete data", hint: "Not built yet" }] },
];
const RIGHT: Group[] = [
  { title: "Platform", tiles: [
    { id: "landscape", label: "Landscape", hint: "Systems, connections and write access" },
    { id: "benchmarks", label: "Benchmarks", hint: "Measured and predicted durations" },
    { id: "agents", label: "AI agents", hint: "Advice only; they cannot approve or write" }] },
];

export default function Solutions() {
  const app = useApp();
  const column = (groups: Group[]) => (
    <div className="sol-col">
      {groups.map((g) => (
        <section key={g.title} className="sol-group" aria-labelledby={`sol-${g.title.replace(/\W+/g, "-")}`}>
          <h2 id={`sol-${g.title.replace(/\W+/g, "-")}`} className="sol-title">{g.title}</h2>
          <div className="sol-tiles">
            {g.tiles.map((t) => {
              const blocked = !!t.perm && !!app.me && !app.can(t.perm);
              return t.id ? (
                <button key={t.label} className="sol-tile" disabled={blocked} onClick={() => app.go(t.id!)} title={blocked ? `requires ${t.perm}` : ""}>
                  <span className="sol-name">{t.label}</span><span className="sol-hint">{t.hint}</span>
                </button>
              ) : (
                <button key={t.label} className="sol-tile" disabled aria-disabled="true">
                  <span className="sol-name">{t.label}</span><span className="sol-hint">Not built yet</span>
                </button>
              );
            })}
          </div>
        </section>))}
    </div>
  );
  return (
    <>
      <div className="eyebrow">Solutions</div>
      <h2 className="hero">Every tool in one place</h2>
      <p className="lede">Pick a tool. Greyed tiles are functions the platform does not have yet.</p>
      <div className="sol-grid">
        {column(LEFT)}
        {column(MIDDLE)}
        {column(RIGHT)}
      </div>
    </>
  );
}

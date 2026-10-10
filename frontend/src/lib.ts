/** Small pure helpers shared by the Transform Factory pages (unit-tested in lib.test.ts). */

export type Band = "LOW" | "MEDIUM" | "HIGH";

/** Shared-data exposure of a scope from its classification counts: the share of objects that are shared,
 *  referenced or need a manual disposition. */
export function sharedRisk(byClassification: Record<string, number> | undefined | null): { band: Band; share: number; shared: number; total: number } {
  const c = byClassification || {};
  const total = Object.values(c).reduce((a, b) => a + (Number(b) || 0), 0);
  const shared = ["SHARED_DUPLICATED", "REFERENCE_ONLY", "MANUAL_DISPOSITION"].reduce((a, k) => a + (Number(c[k]) || 0), 0);
  const share = total ? shared / total : 0;
  return { band: share < 0.1 ? "LOW" : share < 0.3 ? "MEDIUM" : "HIGH", share, shared, total };
}

/** Number of documents that touch more than one company code (the partially transferred bucket). */
export function crossCompany(byClassification: Record<string, number> | undefined | null): number {
  return Number((byClassification || {})["PARTIALLY_TRANSFERRED"]) || 0;
}

/** Bytes as a terabyte figure with one decimal, for the executive tiles. */
export function terabytes(bytes: number | undefined | null): string {
  if (!bytes || bytes <= 0) return "0.0";
  return (bytes / 1024 ** 4).toFixed(1);
}

/** Database size with the unit that keeps the figure readable (TB above 0.1 TB, else GB, else MB). */
export function dbSize(bytes: number | undefined | null): { value: string; unit: "TB" | "GB" | "MB" } {
  const b = bytes && bytes > 0 ? bytes : 0;
  if (b >= 0.1 * 1024 ** 4) return { value: (b / 1024 ** 4).toFixed(1), unit: "TB" };
  if (b >= 0.1 * 1024 ** 3) return { value: (b / 1024 ** 3).toFixed(1), unit: "GB" };
  return { value: (b / 1024 ** 2).toFixed(1), unit: "MB" };
}

/** The audit verdict of a run: the vertical slice is complete when the run completed and nothing failed. */
export function auditVerdict(run: { status?: string; reconciliation?: string | null } | null | undefined): string {
  if (!run) return "NO_RUN";
  if (run.status !== "COMPLETED") return run.status || "UNKNOWN";
  if (run.reconciliation === "FAIL") return "VARIANCES_OPEN";
  return "VERTICAL_SLICE_COMPLETE";
}

export function reconciliationStatus(overall: string | null | undefined): { label: string; cls: "ok" | "warn" | "bad" } {
  if (overall === "PASS") return { label: "RECONCILED", cls: "ok" };
  if (overall === "WARN") return { label: "RECONCILED WITH WARNINGS", cls: "warn" };
  if (overall === "FAIL") return { label: "VARIANCES", cls: "bad" };
  return { label: "NOT RECONCILED", cls: "warn" };
}

export const NAV: { id: string; label: string; icon: string; items: { to: string; label: string; title: string; subtitle: string }[] }[] = [
  { id: "dashboard", label: "Dashboard", icon: "grid", items: [
    { to: "/", label: "Executive dashboard", title: "Executive dashboard", subtitle: "" },
    { to: "/portfolio", label: "Portfolio", title: "Migration factory portfolio", subtitle: "Every project, its systems and runs" },
    { to: "/copilot", label: "Copilot", title: "AI transformation copilot", subtitle: "Bounded agents that propose; humans decide" },
  ] },
  { id: "landscape", label: "Landscape", icon: "building", items: [
    { to: "/landscape", label: "Landscape explorer", title: "Landscape explorer", subtitle: "Read-only organizational discovery" },
    { to: "/analyzer", label: "Enterprise analyzer", title: "Enterprise analyzer", subtitle: "Table statistics, custom code and S/4HANA impacts" },
    { to: "/org", label: "Organizational structure", title: "Organizational structure", subtitle: "Controlling areas, company codes, plants and organisations" },
    { to: "/catalog", label: "Business object catalog", title: "Business object catalog", subtitle: "Canonical objects, tables, keys and load methods" },
  ] },
  { id: "carveout", label: "Carve-out", icon: "factory", items: [
    { to: "/carveout", label: "Carve-out studio", title: "Carve-out studio", subtitle: "ParentCo / SpinCo separation" },
    { to: "/scope", label: "Scope designer", title: "Selective scope designer", subtitle: "Full scope definition, impact preview and versioned manifests" },
    { to: "/bluefield", label: "Bluefield", title: "Bluefield transformation studio", subtitle: "Selective transition with historical data" },
    { to: "/merger", label: "Merger", title: "Merger and consolidation", subtitle: "Multi-source merge groups and key collisions" },
  ] },
  { id: "dependencies", label: "Dependencies", icon: "graph", items: [
    { to: "/graph", label: "Dependency graph", title: "Dependency graph", subtitle: "Ownership, references, and shared edges" },
  ] },
  { id: "connect", label: "Connect", icon: "plug", items: [
    { to: "/connect", label: "System connections", title: "System connections", subtitle: "" },
  ] },
  { id: "extract", label: "Extract", icon: "extract", items: [
    { to: "/extract", label: "ABAP extraction", title: "ABAP extraction", subtitle: "Read-only agents select the headers, lines, open items, assets and valuation of the company code through the add-on. No application-table bypass." },
    { to: "/runs", label: "Run monitor", title: "Extraction and load monitor", subtitle: "Partitioned extraction, transformation, idempotent load, cockpit export" },
    { to: "/quality", label: "Data quality", title: "Data quality dashboard", subtitle: "Rule exceptions and quality findings" },
  ] },
  { id: "rules", label: "Rules", icon: "scale", items: [
    { to: "/rules", label: "Transformation rules", title: "Transformation rules", subtitle: "Declarative, deterministic rules with embedded tests. A dry run rewrites keys and organisational values on a source sample; amounts stay unchanged." },
  ] },
  { id: "cdc", label: "CDC", icon: "clock", items: [
    { to: "/delta", label: "Near-zero downtime", title: "Near-zero downtime", subtitle: "Delta cycles through the released APIs, freeze and final delta. No downtime figure is claimed: the window is the final delta plus the reconciliation." },
  ] },
  { id: "finance", label: "Finance", icon: "bank", items: [
    { to: "/reconciliation", label: "Financial reconciliation", title: "Financial reconciliation", subtitle: "Trial balance, GL balances, open items, assets, inventory and intercompany, compared through the adapters; technical and functional layers alongside" },
  ] },
  { id: "cutover", label: "Cutover", icon: "clipboard-check", items: [
    { to: "/cutover", label: "Production cutover", title: "Production cutover", subtitle: "Technical and business gates stay separate. After the target posting period opens, recovery is forward-only." },
  ] },
  { id: "audit", label: "Audit", icon: "clipboard", items: [
    { to: "/audit", label: "Audit report", title: "Audit report", subtitle: "" },
    { to: "/compliance", label: "Compliance and evidence", title: "Compliance and evidence center", subtitle: "Hash-chained audit trail, approvals, evidence packages" },
  ] },
];

export function groupFor(pathname: string) {
  return NAV.find((g) => g.items.some((i) => i.to === pathname)) || NAV[0];
}

export function pageFor(pathname: string) {
  return NAV.flatMap((g) => g.items).find((i) => i.to === pathname);
}

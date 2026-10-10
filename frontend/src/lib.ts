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
    { to: "/journey", label: "Transformation journey", title: "Transformation journey", subtitle: "" },
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

// ---- transformation journey (original phase model; statuses derived from the platform state)
export type DeliverableStatus = "DONE" | "IN_PROGRESS" | "PENDING";
export type Deliverable = { label: string; status: DeliverableStatus; detail?: string; to?: string };
export type Phase = { id: string; title: string; lead: string; status: DeliverableStatus; deliverables: Deliverable[] };

export function phaseStatus(items: { status: DeliverableStatus }[]): DeliverableStatus {
  if (!items.length) return "PENDING";
  if (items.every((d) => d.status === "DONE")) return "DONE";
  if (items.some((d) => d.status !== "PENDING")) return "IN_PROGRESS";
  return "PENDING";
}

export type JourneyInput = { discovery: boolean; graph: boolean; manifests: any[]; rulesets: any[]; runs: any[]; completeness: any; rehearsals: any[]; delta: any; evidence: any; approvals: any[]; auditChain: boolean | null };

export function journeyPhases(i: JourneyInput): Phase[] {
  const approvedM = i.manifests.find((m) => m.status === "APPROVED");
  const validR = i.rulesets.find((r) => r.validation?.ok);
  const approvedR = i.rulesets.find((r) => r.status === "APPROVED");
  const completed = i.runs.filter((r) => r.status === "COMPLETED" && r.metrics?.kind !== "DELTA");
  const latestRun = completed[0];
  const recon = latestRun?.reconciliation;
  const pendingDisp = i.completeness?.pending_approvals?.count;
  const cycles: any[] = i.delta?.cycles || [];
  const go = i.rehearsals.find((r) => r.verdict === "GO");
  const businessSignoff = i.approvals.find((a) => a.kind === "BUSINESS" && a.decision === "APPROVED");
  const technicalSignoff = i.approvals.find((a) => a.kind === "TECHNICAL" && a.decision === "APPROVED");
  const s = (done: boolean, progress = false): DeliverableStatus => (done ? "DONE" : progress ? "IN_PROGRESS" : "PENDING");
  const mk = (id: string, title: string, lead: string, deliverables: Deliverable[]): Phase => ({ id, title, lead, status: phaseStatus(deliverables), deliverables });
  return [
    mk("discover", "Discover and scope", "Know the landscape before deciding what moves.", [
      { label: "Landscape snapshot", status: s(i.discovery), detail: i.discovery ? "discovery run on the source" : "run discovery (Connect)", to: "/landscape" },
      { label: "Dependency graph", status: s(i.graph), detail: i.graph ? "built from the discovered objects" : "build it (Dependencies)", to: "/graph" },
      { label: "Scope manifest", status: s(!!i.manifests.length), detail: i.manifests.length ? `${i.manifests.length} version(s), hashed` : "generate one (Carve-out)", to: "/carveout" },
    ]),
    mk("design", "Analyse and design", "Decide the ownership of every shared object and the rules that rewrite the data.", [
      { label: "Business dispositions", status: s(!!i.manifests.length && pendingDisp === 0, !!i.manifests.length && pendingDisp > 0), detail: pendingDisp === undefined ? "" : pendingDisp === 0 ? "none pending" : `${pendingDisp} pending`, to: "/carveout" },
      { label: "Manifest approved (four-eyes)", status: s(!!approvedM, !!i.manifests.length), detail: approvedM ? `${approvedM.name} v${approvedM.version} by ${approvedM.approved_by}` : "approver decision outstanding", to: "/carveout" },
      { label: "Rule set validated", status: s(!!validR, !!i.rulesets.length), detail: validR ? `${validR.name} v${validR.version}: ${validR.rule_count} rules, tests pass` : "generate and validate (Rules)", to: "/rules" },
    ]),
    mk("simulate", "Transform and simulate", "Run the whole pipeline against the simulators until the checks agree.", [
      { label: "Rule set approved", status: s(!!approvedR, !!validR), detail: approvedR ? `by ${approvedR.approved_by}` : "", to: "/rules" },
      { label: "Simulated run completed", status: s(!!latestRun, i.runs.some((r) => r.status === "RUNNING")), detail: latestRun ? `${completed.length} completed run(s)` : "run the extraction agents (Extract)", to: "/extract" },
      { label: "Consistency checks", status: s(recon === "PASS", recon === "WARN"), detail: recon ? `reconciliation ${recon}` : "", to: "/reconciliation" },
    ]),
    mk("execute", "Execute and cut over", "Keep the target in sync and rehearse until the gates are green.", [
      { label: "Delta cycles", status: s(cycles.some((c) => c.status === "COMPLETED"), !!latestRun), detail: cycles.length ? `${cycles.length} cycle(s)` : "", to: "/delta" },
      { label: "Final delta and reconciliation", status: s(!!i.delta?.cutover_ready, !!i.delta?.freeze), detail: i.delta?.cutover_ready ? "cutover ready" : i.delta?.freeze ? "freeze declared" : "", to: "/delta" },
      { label: "Go-live gates", status: s(!!go, i.rehearsals.length > 0), detail: go ? `GO on ${go.name}` : i.rehearsals.length ? `${i.rehearsals.length} rehearsal(s)` : "create a rehearsal (Cutover)", to: "/cutover" },
    ]),
    mk("govern", "Govern and sign off", "Prove what happened and let the business accept it.", [
      { label: "Evidence package", status: s(!!i.evidence?.evidence?.files && Object.keys(i.evidence.evidence.files).length > 0, !!latestRun), detail: i.evidence?.evidence?.files ? `${Object.keys(i.evidence.evidence.files).length} files, SHA-256` : "", to: "/audit" },
      { label: "Audit chain verified", status: s(i.auditChain === true, i.auditChain === null && !!latestRun), detail: i.auditChain === true ? "hash chain intact" : i.auditChain === false ? "CHAIN BROKEN" : "auditor role sees the chain", to: "/compliance" },
      { label: "Technical and business sign-off", status: s(!!businessSignoff && !!technicalSignoff, !!businessSignoff || !!technicalSignoff), detail: [technicalSignoff && "technical", businessSignoff && "business"].filter(Boolean).join(" + ") || "sign off the run (Finance)", to: "/reconciliation" },
    ]),
  ];
}

export const APPROACHES: { id: string; title: string; summary: string; scenarios: string[]; platform: string[] }[] = [
  { id: "conversion", title: "System conversion", summary: "The production system is converted in place; data stays where it is and is simplified by the conversion.", scenarios: [], platform: ["Discovery with the S/4HANA data-model impacts per table (compatibility views, replaced tables, Material Ledger)", "Custom-table and interface inventory for the pre-conversion checks", "Reconciliation of the converted system against the snapshot taken before"] },
  { id: "new", title: "New implementation", summary: "A newly configured S/4HANA system; only selected master and open data move into it.", scenarios: ["CARVE_OUT", "MERGER"], platform: ["Selective scope with classification and business dispositions", "Transformation rules and the load through the released APIs or the migration cockpit packages", "Three-layer reconciliation and the evidence package"] },
  { id: "selective", title: "Selective data transition", summary: "A copy of the system is emptied of its transactional data and used as the target; the selected history and open data are moved into it.", scenarios: ["SDT", "BLUEFIELD"], platform: ["Historical policy per scope (full history, open items and balances, fiscal years)", "Carve-out of company codes with the shared objects duplicated, referenced or excluded", "Delta cycles and the final delta for a near-zero-downtime window"] },
];

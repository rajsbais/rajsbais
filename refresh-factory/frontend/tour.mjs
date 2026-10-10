import { chromium } from "playwright";
const BASE = "http://localhost:8096";
const b = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium", args: ["--no-sandbox"] });
const ctx = await b.newContext({ colorScheme: "light", viewport: { width: 1360, height: 900 } });
const H = (u) => ({ "X-Demo-User": u, "Content-Type": "application/json" });
const api = async (method, path, user, body) => {
  const r = await ctx.request.fetch(BASE + path, { method, headers: H(user), data: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  if (!r.ok()) throw new Error(`${method} ${path} -> ${r.status()} ${t.slice(0, 200)}`);
  return t ? JSON.parse(t) : null;
};
await api("POST", "/api/demo/bootstrap", "root.admin");
const sys = Object.fromEntries((await api("GET", "/api/systems", "alice.basis")).map((s) => [s.sid, s.id]));
async function refresh(name, scope, down, plants = []) {
  const p = await api("POST", "/api/projects", "alice.basis", { name, source_id: sys.EP1, target_id: sys.EQ1 });
  await api("PUT", `/api/projects/${p.id}/manifest`, "alice.basis", { scope: { ...scope, plants }, last_days: scope.object_type === "SALES_ORDER" ? 90 : null, include_downstream: down, masking_policy_id: "gdpr-standard", conflict_policy: {}, instance_overrides: {} });
  await api("POST", `/api/projects/${p.id}/plan`, "alice.basis");
  const adv = await api("GET", `/api/projects/${p.id}/agents/masking`, "alice.basis");
  if (adv.add_rules.length) await api("POST", `/api/projects/${p.id}/masking/rules`, "alice.basis", { rules: adv.add_rules });
  await api("PUT", `/api/projects/${p.id}/conflicts/policy`, "alice.basis", { conflict_policy: { DUPLICATE_DIFFERENT: "SKIP" } });
  await api("POST", `/api/projects/${p.id}/plan`, "alice.basis");
  await api("POST", `/api/projects/${p.id}/conflicts/analyze`, "alice.basis");
  await api("POST", `/api/projects/${p.id}/submit`, "alice.basis");
  await api("POST", `/api/projects/${p.id}/approve`, "carol.approver", { comment: "approved for the tour" });
  const run = await api("POST", `/api/projects/${p.id}/execute`, "alice.basis");
  return { project: p.id, run: run.id };
}
const a = await refresh("CC1000 sales orders, last 90 days", { object_type: "SALES_ORDER", company_codes: ["1000"] }, ["DELIVERY", "BILLING", "FI_DOCUMENT"]);
const w = await refresh("Warehouse transfer orders, plant 1000", { object_type: "TRANSFER_ORDER", company_codes: [] }, [], ["1000"]);
// a project prepared but not submitted, for the designer screens
const open = await api("POST", "/api/projects", "alice.basis", { name: "Maintenance orders, plant 1000 (in preparation)", source_id: sys.EP1, target_id: sys.EQ1 });
await api("PUT", `/api/projects/${open.id}/manifest`, "alice.basis", { scope: { object_type: "MAINT_ORDER", company_codes: [], plants: ["1000"] }, last_days: null, include_downstream: [], masking_policy_id: "gdpr-standard", conflict_policy: {}, instance_overrides: {} });
await api("POST", `/api/projects/${open.id}/plan`, "alice.basis");
await api("POST", `/api/projects/${open.id}/conflicts/analyze`, "alice.basis");

const page = await ctx.newPage();
await page.addInitScript(([pr, ru]) => { try { localStorage.setItem("rf.project", pr); localStorage.setItem("rf.run", ru); localStorage.setItem("rf.user", "alice.basis"); } catch {} }, [a.project, a.run]);
await page.goto(BASE);
await page.waitForTimeout(1200);
const nav = (label) => page.getByRole("navigation", { name: "Main" }).getByRole("button", { name: label, exact: true });
const shots = [];
async function shot(id, label, before) {
  await nav(label).click();
  await page.waitForTimeout(500);
  if (before) await before();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `/tmp/tour/${id}.jpg`, type: "jpeg", quality: 80, fullPage: true });
  shots.push(id);
}
const asUser = async (u) => { await page.locator("#user").selectOption(u); await page.waitForTimeout(500); };
await shot("solutions", "Solutions");
await shot("guided", "Guided refresh");
await shot("tower", "Control tower");
await shot("landscape", "Landscape");
await shot("readiness", "Readiness");
await shot("roadmap", "Roadmap");
await shot("designer", "Selective designer", async () => { await page.getByRole("tab", { name: "Plan preview" }).click(); });
await shot("delta", "Delta refresh");
await shot("dependencies", "Dependencies");
await shot("conflicts", "Conflicts");
await shot("masking", "Masking");
await shot("execution", "Execution");
await shot("reconciliation", "Reconciliation");
await shot("lean", "Lean client");
await shot("fullrefresh", "Full refresh");
await shot("postcopy", "Post-copy");
await shot("orchestration", "Orchestration");
await shot("benchmarks", "Benchmarks", async () => { await page.getByRole("button", { name: "Run benchmark on source" }).click().catch(() => {}); await page.waitForTimeout(2500); });
await shot("analysis", "Data analysis", async () => {
  await page.getByLabel("System to analyse").selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Ready-made analysis/).selectOption({ label: "Production and maintenance orders by order type" });
  await page.getByRole("button", { name: "Analyse" }).click();
  await page.waitForTimeout(800);
});
await shot("catalog", "Test catalog");
await asUser("root.admin");
await shot("audit", "Audit");
await asUser("alice.basis");
await shot("agents", "AI agents");
console.log(JSON.stringify(shots));
await b.close();

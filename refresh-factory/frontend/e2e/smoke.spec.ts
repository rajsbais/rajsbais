import { expect, test } from "./fixtures";

const ALL_VIEWS = ["Solutions", "Guided refresh", "Data analysis", "Control tower", "Landscape", "Readiness", "Roadmap", "Selective designer", "Delta refresh", "Dependencies", "Conflicts", "Masking", "Execution",
  "Reconciliation", "Lean client", "Full refresh", "Post-copy", "Orchestration", "Benchmarks", "Test catalog", "Audit", "AI agents"];

test("the app says it is simulated and every view opens without console or page errors", async ({ app }) => {
  const { page } = app;
  await expect(page.getByText("Simulated SAP")).toBeVisible();
  await app.loadLandscape();
  await app.as("root.admin");
  for (const v of ALL_VIEWS) {
    await app.nav(v);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(v);
  }
  expect(app.errors).toEqual([]);
});

test("the landscape lists ECC and S/4HANA systems and never offers production as a write target", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Landscape");
  for (const sid of ["EP1/100", "EQ1/200", "S4P/100", "S4Q/200"]) await expect(page.getByText(sid).first()).toBeVisible();
  await app.nav("Lean client");
  await expect(page.getByLabel("Host system", { exact: true }).locator("option", { hasText: "PRD" })).toHaveCount(0);
});

test("what each role may do is reflected in the UI", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  const nav = page.getByRole("navigation", { name: "Main" });
  await app.as("erin.auditor");
  await expect(nav.getByRole("button", { name: "Audit", exact: true })).toBeEnabled();
  await app.as("tina.tester");
  await expect(nav.getByRole("button", { name: "Audit", exact: true })).toBeDisabled();
  await app.nav("Selective designer");
  await expect(page.getByRole("button", { name: "Create project" })).toBeDisabled();
  await app.nav("Orchestration");
  await expect(page.getByRole("button", { name: "Submit demo pipeline" })).toBeDisabled();
  await app.nav("Full refresh");
  await expect(page.getByRole("button", { name: "Create program" })).toBeDisabled();
  await app.as("refresh.copilot");
  await app.nav("Orchestration");
  await expect(page.getByRole("button", { name: "Submit demo pipeline" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Run scheduler tick" })).toBeEnabled(); // the UI offers it; the API refuses agents (see backend tests)
  await expect(page.getByText("[AI agent]").first()).toBeAttached();
});

test("the API refuses what the UI hides: an agent principal cannot approve, release or tick", async ({ app }) => {
  const agent = app.api("refresh.copilot");
  expect((await agent.post("/api/orchestration/tick")).status).toBe(403);
  expect((await agent.post("/api/orchestration/jobs", { kind: "tdm_sweep" })).status).toBe(403);
  expect((await agent.post("/api/full-refresh/programs/none/release")).status).toBe(403);
  const me = (await agent.get("/api/me")).body;
  expect(me.kind).toBe("agent");
  expect(me.permissions).not.toContain("plan:approve");
});

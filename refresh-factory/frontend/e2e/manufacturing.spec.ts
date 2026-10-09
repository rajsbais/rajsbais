import { expect, expectAccessible, test } from "./fixtures";

test("manufacturing: design a production-order refresh, see the BOM and goods movements in the plan, execute and reconcile", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Selective designer");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  await page.getByLabel("Root business object").selectOption("PRODUCTION_ORDER");
  await page.getByRole("checkbox", { name: "MATERIAL_DOCUMENT" }).check();
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expectAccessible(page, "designer: production-order plan");
  const project = (await app.api("alice.basis").get("/api/projects")).body[0];
  const inst = await app.api("alice.basis").get(`/api/projects/${project.id}/instances?type=BOM&limit=50`);
  expect(inst.body.total).toBeGreaterThan(0); // the BOMs came along as required objects

  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByLabel(/exists in the target/).selectOption("SKIP");
  await app.click("Apply policy and re-analyze");
  await page.getByRole("tab", { name: "Masking" }).click();
  await expect(page.getByRole("button", { name: /Apply advisor/ })).toBeDisabled(); // manufacturing data holds nothing the masking catalog flags
  await page.getByRole("tab", { name: "Approval" }).click();
  await app.click("Submit for approval");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await page.getByRole("tab", { name: "Execute" }).click();
  await app.click("Execute selective refresh");
  await expect(page.getByText("RELEASED").first()).toBeVisible();
  const run = (await app.api("alice.basis").get(`/api/projects/${project.id}`)).body.runs.at(-1);
  const rec = (await app.api("alice.basis").get(`/api/runs/${run}/reconciliation`)).body;
  const pp = rec.checks.filter((c: { id: string }) => c.id.startsWith("BUS-PP-"));
  expect(pp.map((c: { id: string }) => c.id).sort()).toEqual(["BUS-PP-BOM", "BUS-PP-COMPONENTS", "BUS-PP-MOVEMENTS", "BUS-PP-STRUCT"]);
  expect(pp.every((c: { status: string }) => c.status === "pass")).toBe(true);
  await expectAccessible(page, "reconciliation with manufacturing checks");
  expect(app.errors).toEqual([]);
});

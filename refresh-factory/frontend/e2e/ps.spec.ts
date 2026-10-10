import { expect, expectAccessible, test } from "./fixtures";

test("project system: refresh a project with its WBS hierarchy and costs, mask the owners, reconcile", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Selective designer");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  await page.getByLabel("Root business object").selectOption("PROJECT");
  await page.getByLabel("Company codes (comma separated)").fill(""); // projects are scoped by company code or plant
  await page.getByLabel("Created in the last N days (0 = no date filter)").fill("0");
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expectAccessible(page, "designer: project plan");
  const project = (await app.api("alice.basis").get("/api/projects")).body[0];
  const types = (await app.api("alice.basis").get(`/api/projects/${project.id}/plan`)).body.by_type;
  expect(Object.keys(types).sort()).toEqual(["PROJECT"]);

  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByRole("tab", { name: "Masking" }).click();
  await expect(page.getByRole("button", { name: /Apply advisor/ })).toBeDisabled(); // the standard policy already covers the owners: nothing is left to add
  const adv = (await app.api("alice.basis").get(`/api/projects/${project.id}/masking`)).body;
  expect(adv.uncovered).toBe(0);
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
  const byId = Object.fromEntries(rec.checks.map((c: { id: string; status: string }) => [c.id, c.status]));
  expect(byId["BUS-PS-REFS"]).toBe("pass");
  expect(byId["SEC-RESIDUAL"]).toBe("pass");
  await expectAccessible(page, "reconciliation with project checks");
  expect(app.errors).toEqual([]);
});

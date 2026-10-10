import { expect, expectAccessible, test } from "./fixtures";

test("HR data: only an HR steward can select it, stable pseudonyms are refused, per-run anonymization works end to end", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Selective designer");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  await page.getByLabel("Root business object").selectOption("EMPLOYEE");
  await app.click(/Save manifest/);
  await expect(page.getByRole("alert")).toContainText(/hr:copy is required/); // alice (basis) may not select HR data

  await app.as("hanna.hr"); // the HR steward takes over the same project
  await app.nav("Selective designer");
  await page.getByRole("tab", { name: "Scope" }).click();
  await page.getByLabel("Root business object").selectOption("EMPLOYEE");
  await page.getByLabel("Company codes (comma separated)").fill("1000");
  await page.getByLabel("Created in the last N days (0 = no date filter)").fill("0"); // employees have no creation-date filter
  await app.click(/Save manifest/); // default masking is the standard (pseudonymizing) policy
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByRole("tab", { name: "Approval" }).click();
  await app.click("Submit for approval");
  await expect(page.getByRole("alert")).toContainText(/per-run anonymization/); // refused with the fields named
  await expectAccessible(page, "HR plan refused: stable pseudonyms");

  await page.getByRole("tab", { name: "Scope" }).click();
  await page.getByLabel("Masking template").selectOption("gdpr-strict");
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByRole("tab", { name: "Approval" }).click();
  await app.click("Submit for approval");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("hanna.hr");
  await page.getByRole("tab", { name: "Execute" }).click();
  await app.click("Execute selective refresh");
  await expect(page.getByText("RELEASED").first()).toBeVisible();
  const project = (await app.api("alice.basis").get("/api/projects")).body.at(-1);
  const run = (await app.api("alice.basis").get(`/api/projects/${project.id}`)).body.runs.at(-1);
  const rec = (await app.api("alice.basis").get(`/api/runs/${run}/reconciliation`)).body;
  const byId = Object.fromEntries(rec.checks.map((c: { id: string; status: string }) => [c.id, c.status]));
  expect(byId["BUS-HR-REFS"]).toBe("pass");
  expect(byId["SEC-HR-ANON"]).toBe("pass");
  expect(byId["SEC-RESIDUAL"]).toBe("pass");
  await expectAccessible(page, "HR refresh released");
});

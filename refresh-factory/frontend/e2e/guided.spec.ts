import { expect, expectAccessible, test } from "./fixtures";

test("guided refresh: from choosing an area to a released result, with approval by a different person", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Guided refresh");
  await expectAccessible(page, "guided refresh: step 1");
  await page.getByLabel(/^From/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Into/).selectOption({ label: "EQ1/200 (QAS)" });
  await page.getByRole("radio", { name: /Maintenance orders/ }).check();
  await app.click("Continue");

  await expect(page.getByRole("heading", { name: "2 · Prepare the refresh" })).toBeVisible();
  await app.click("Prepare");
  await expect(page.getByText("Business objects").first()).toBeVisible();
  await expect(page.getByText("Unmasked sensitive fields").first()).toBeVisible();
  await expectAccessible(page, "guided refresh: prepared");

  await expect(page.getByRole("heading", { name: "3 · Review and submit" })).toBeVisible();
  await app.click("Submit for approval");

  await expect(page.getByRole("heading", { name: "4 · Approval" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toBeDisabled(); // the person who prepared and submitted it cannot approve
  await app.as("carol.approver");
  await app.click("Approve");

  await app.as("alice.basis");
  await expect(page.getByRole("heading", { name: "5 · Run the refresh" })).toBeVisible();
  await app.click("Run the refresh");
  await expect(page.getByRole("heading", { name: "6 · Result" })).toBeVisible();
  await expect(page.getByText("RELEASED").first()).toBeVisible();
  await expect(page.getByText(/\d+ \/ \d+ passed/)).toBeVisible();
  await expectAccessible(page, "guided refresh: result");

  const project = (await app.api("alice.basis").get("/api/projects")).body[0];
  const run = (await app.api("alice.basis").get(`/api/projects/${project.id}`)).body.runs.at(-1);
  const rec = (await app.api("alice.basis").get(`/api/runs/${run}/reconciliation`)).body;
  expect(Object.fromEntries(rec.checks.map((c: { id: string; status: string }) => [c.id, c.status]))["BUS-PM-REFS"]).toBe("pass");

  await app.click("Start another refresh");
  await expect(page.getByRole("heading", { name: "1 · What do you want to refresh?" })).toBeVisible();
  expect(app.errors).toEqual([]);
});

test("guided refresh: the sales scenario prepares with masking added automatically, and roles without permission are held back", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Guided refresh");
  await page.getByLabel(/^From/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Into/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Continue");
  await app.click("Prepare");
  await expect(page.getByRole("heading", { name: "3 · Review and submit" })).toBeVisible();
  const project = (await app.api("alice.basis").get("/api/projects")).body[0];
  const mask = (await app.api("alice.basis").get(`/api/projects/${project.id}/masking`)).body;
  expect(mask.uncovered).toBe(0); // the advisor's rules were added for the personal data the sales documents carry
  await expect(page.getByText(/Nothing blocks this refresh/)).toBeVisible();

  await app.as("erin.auditor"); // an auditor may look but not submit
  await expect(page.getByRole("button", { name: "Submit for approval" })).toBeDisabled();
  await app.as("alice.basis");
  await app.click("Submit for approval");
  await app.as("erin.auditor");
  await expect(page.getByRole("button", { name: "Approve" })).toBeDisabled();
  await expect(page.getByText("Your role cannot approve.")).toBeVisible();
  expect(app.errors).toEqual([]);
});

test("guided refresh: a role that cannot create projects cannot continue, and HR data says what it needs", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Guided refresh");
  await page.getByRole("radio", { name: /Employees/ }).check();
  await app.as("erin.auditor");
  await expect(page.getByRole("button", { name: "Continue" })).toBeDisabled();
  await expect(page.getByText(/hr:copy/)).toBeVisible();
});

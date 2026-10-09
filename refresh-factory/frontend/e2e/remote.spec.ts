import { expect, expectAccessible, test } from "./fixtures";

test("a remote read-only source (fake RFC transport) can be connected, inspected and used as the source of a refresh", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Landscape");
  await app.click("Connect demo remote source (fake RFC)");
  await expect(page.getByText("remote (RFC)")).toBeVisible();
  await expect(page.getByText(/row counts need full scans/)).toBeVisible();
  await expect(page.getByText("not validated against a real SAP system")).toBeVisible();
  await expect(page.getByText(/The modelled DDIC fields and keys all exist remotely/)).toBeVisible();
  await expectAccessible(page, "landscape with a remote source");

  await app.nav("Selective designer");
  await expect(page.getByLabel(/^Target/).locator("option", { hasText: "EP2" })).toHaveCount(0); // a remote system is never offered as a target
  await page.getByLabel(/^Source/).selectOption({ label: "EP2/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByLabel(/exists in the target/).selectOption("SKIP");
  await app.click("Apply policy and re-analyze");
  await page.getByRole("tab", { name: "Masking" }).click();
  await app.click(/Apply advisor/);
  await page.getByRole("tab", { name: "Approval" }).click();
  await app.click("Submit for approval");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await page.getByRole("tab", { name: "Execute" }).click();
  await app.click("Execute selective refresh");
  await expect(page.getByText("RELEASED").first()).toBeVisible();

  await app.nav("Landscape");
  await page.getByRole("button", { name: /EP2\/100/ }).click();
  await expect(page.getByText(/Calls \d+/)).toBeVisible();
  const remote = (await app.api("erin.auditor").get("/api/systems")).body.find((s: { sid: string }) => s.sid === "EP2");
  const info = (await app.api("erin.auditor").get(`/api/systems/${remote.id}/remote`)).body;
  expect(info.stats.calls).toBeGreaterThan(20);
  expect(info.stats.guard_trips).toBe(0);
  expect(app.errors).toEqual([]);
});

import { expect, expectAccessible, test } from "./fixtures";

test("data analysis: distribution with coverage, selectivity, growth, table-to-object view; personal data is never grouped", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Data analysis");
  await page.getByLabel("System to analyse").selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Ready-made analysis/).selectOption({ label: "Production and maintenance orders by order type" });
  await app.click("Analyse");
  await expect(page.getByText("Cover 80%")).toBeVisible();
  await expect(page.getByRole("cell", { name: "PM01" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "PP01" }).first()).toBeVisible();
  await expectAccessible(page, "data analysis: distribution");

  // a personal-data field cannot even be ticked for a distribution, and the API refuses it anyway
  await page.getByLabel(/^Table/).selectOption("KNA1");
  await expect(page.getByRole("checkbox", { name: /NAME1/ })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "STCD1 (personal data)" })).toBeDisabled();
  const refused = await app.api("alice.basis").post(`/api/systems/${(await app.api("alice.basis").get("/api/systems")).body.find((s: { sid: string }) => s.sid === "EP1").id}/analysis/distribution`, { table: "KNA1", fields: ["NAME1"] });
  expect(refused.status).toBe(409);
  expect(JSON.stringify(refused.body)).toContain("personal data");

  await page.getByRole("tab", { name: "Selectivity" }).click();
  await page.getByLabel(/^Table/).selectOption("QMEL");
  await page.getByRole("checkbox", { name: "QMART" }).check();
  await app.click("Analyse");
  await expect(page.getByText("low selectivity")).toBeVisible();
  await page.getByRole("tab", { name: "Growth" }).click();
  await page.getByLabel(/^Ready-made analysis/).selectOption({ index: 1 });
  await app.click("Analyse");
  await expect(page.getByText("Busiest")).toBeVisible();

  await page.getByRole("tab", { name: "Tables and objects" }).click();
  await expect(page.getByRole("cell", { name: "MAINT_ORDER" }).first()).toBeVisible();
  await page.getByRole("tab", { name: "Not available" }).click();
  await expect(page.getByText(/ST03N/)).toBeVisible();
  await expectAccessible(page, "data analysis: not available");
  expect(app.errors).toEqual([]);
});

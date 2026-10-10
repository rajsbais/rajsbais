import { expect, expectAccessible, test } from "./fixtures";

test("the solutions launcher opens each tool, and shows what is not built instead of hiding it", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Solutions");
  await expectAccessible(page, "solutions launcher");
  for (const g of ["Client construct", "Selective data", "Data transform", "Data discard"]) await expect(page.getByRole("heading", { name: g })).toBeVisible();
  for (const missing of ["System build plus", "Delete data"]) await expect(page.getByRole("main").getByRole("button", { name: new RegExp(missing) })).toBeDisabled();
  await page.getByRole("main").getByRole("button", { name: /^Masking/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Masking");
  await app.nav("Solutions");
  await page.getByRole("main").getByRole("button", { name: /^Lean client/ }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Lean client");
  await app.as("erin.auditor");
  await app.nav("Solutions");
  await expect(page.getByRole("main").getByRole("button", { name: /^Audit and evidence/ })).toBeEnabled();
  expect(app.errors).toEqual([]);
});

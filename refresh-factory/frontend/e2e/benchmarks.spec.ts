import { expect, expectAccessible, test } from "./fixtures";

test("benchmark calibrates estimates, keeps environments apart and refuses bad imports", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Benchmarks");
  await expect(page.getByText("No samples yet")).toBeVisible();
  await app.click("Estimate");
  await expect(page.getByText("placeholder").first()).toBeVisible();

  await app.click("Run benchmark on source");
  await expect(page.getByText(/Benchmark added \d+ sample/)).toBeVisible();
  await expect(page.getByText("measured here").first()).toBeVisible();
  await expect(page.getByText("simulator and of the masking engine measure this program on this host, not SAP")).toBeVisible();
  await expectAccessible(page, "benchmarks");

  // the reserved label is refused, a real label is accepted and stays declared
  await page.getByLabel("Environment", { exact: true }).fill("simulated");
  await app.click("Import sample");
  await expect(page.getByRole("alert")).toContainText("reserved");
  await page.getByLabel("Environment", { exact: true }).fill("real:QAS-copy-1");
  await app.click("Import sample");
  await expect(page.getByText("(declared)").first()).toBeVisible();

  // an agent principal cannot import or run
  await app.as("refresh.copilot");
  await expect(page.getByRole("button", { name: "Import sample" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Run benchmark on source" })).toBeDisabled();
});

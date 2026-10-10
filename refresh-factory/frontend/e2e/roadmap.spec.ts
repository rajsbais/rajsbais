import { expect, expectAccessible, test } from "./fixtures";

test("roadmap: what is pending, filtered by status and owner, with the plan of each item", async ({ app }) => {
  const { page } = app;
  await app.nav("Roadmap");
  await expect(page.getByText("What is still pending")).toBeVisible();
  const total = (await app.api("alice.basis").get("/api/roadmap")).body.items.length;
  await expect(page.getByText(`${total} of ${total} item(s) shown`)).toBeVisible();
  await expectAccessible(page, "roadmap");

  await page.getByRole("group", { name: "Filter by status" }).getByRole("button", { name: "Needs you" }).click();
  await expect(page.getByText(/^5 of \d+ item\(s\) shown/)).toBeVisible();
  await expect(page.getByText("R1", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("B14")).toHaveCount(0); // a ready-to-build item is not shown under "Needs you"

  await page.getByRole("group", { name: "Filter by status" }).getByRole("button", { name: "Declined" }).click();
  await expect(page.getByText(/Delete data tool/)).toBeVisible();
  await page.getByText(/Delete data tool/).click();
  await expect(page.getByText(/you asked that no data be deleted/i)).toBeVisible();

  await page.getByRole("group", { name: "Filter by status" }).getByRole("button", { name: "All" }).click();
  await page.getByRole("group", { name: "Filter by owner" }).getByRole("button", { name: "Claude" }).click();
  await expect(page.getByText(/item\(s\) shown/)).toBeVisible();
  await page.getByText(/Security review pass/).click();
  await expect(page.getByText("Done when:").first()).toBeVisible();
  await expectAccessible(page, "roadmap with an item open");
  expect(app.errors).toEqual([]);
});

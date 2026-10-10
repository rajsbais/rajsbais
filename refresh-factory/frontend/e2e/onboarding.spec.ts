import { expect, expectAccessible, Keystone, startBackend, test } from "./fixtures";

// The fake-endpoint hook is switched on for these tests only: hosts ending in ".invalid" are then served by a FAKE endpoint.
test("connect a system from the UI: test first, read the findings, then register it as a read-only source", async ({ page }) => {
  const be = await startBackend({ RFACTORY_ALLOW_FAKE_ENDPOINTS: "1", E2E_SAP_PW: "never-shown" });
  try {
    const app = new Keystone(page, be.url);
    await app.open();
    await app.loadLandscape();
    await app.nav("Landscape");
    const card = page.getByRole("region", { name: "Connect a real system (read-only)" }).or(page.locator("section", { has: page.getByRole("heading", { name: "Connect a real system (read-only)" }) }));
    await expect(card).toBeVisible();
    await expectAccessible(page, "connect-a-system form");

    const run = card.getByRole("button", { name: "Run smoke test" });
    await expect(run).toBeDisabled(); // nothing filled in, nothing confirmed
    await card.getByLabel("System ID (SID)").fill("sbx1");
    await card.getByLabel("Application server host").fill("sandbox.invalid");
    await card.getByLabel("SAP user (read-only)").fill("RFREAD");
    await card.getByLabel("Password environment variable (name)").fill("E2E_SAP_PW");
    await expect(run).toBeDisabled(); // still not confirmed
    await card.getByLabel("This is a sandbox or a copy, and the SAP user is read-only").check();
    await card.getByLabel("Rows read per table (max)").fill("100");
    await card.getByLabel("Calls per minute (throttle)").fill("60000"); // the fake endpoint needs no throttling
    await expect(card.getByRole("button", { name: "Register as read-only source" })).toBeDisabled(); // not before a test
    await run.click();
    await expect(card.getByText("Smoke test result")).toBeVisible();
    await expect(card.getByText(/tables could be read/)).toBeVisible();
    await expect(card.getByRole("cell", { name: "MARA", exact: true })).toBeVisible();
    await expect(card.getByText(/The report contains no row values/)).toBeVisible();
    expect(await page.content()).not.toContain("never-shown");
    await expectAccessible(page, "smoke test result");
    const before = (await app.api("alice.basis").get("/api/systems")).body as { sid: string }[];
    expect(before.some((s) => s.sid === "SBX1")).toBe(false); // testing registered nothing

    await card.getByRole("button", { name: "Register as read-only source" }).click();
    await expect(page.getByText("remote (RFC)").first()).toBeVisible();
    const after = (await app.api("alice.basis").get("/api/systems")).body as { sid: string; adapter: string }[];
    expect(after.find((s) => s.sid === "SBX1")?.adapter).toBe("rfc");
    await expect(page.getByRole("heading", { name: "Remote connection (read-only)" })).toBeVisible();
  } finally {
    await be.stop();
  }
});

test("a missing password variable and an unconfirmed test are explained and nothing runs", async ({ page }) => {
  const be = await startBackend({ RFACTORY_ALLOW_FAKE_ENDPOINTS: "1" });
  try {
    const app = new Keystone(page, be.url);
    await app.open();
    await app.loadLandscape();
    await app.nav("Landscape");
    const card = page.locator("section", { has: page.getByRole("heading", { name: "Connect a real system (read-only)" }) });
    await card.getByLabel("Connection type").selectOption("odata");
    await card.getByLabel("System ID (SID)").fill("S4SBX");
    await card.getByLabel("Base URL (https)").fill("https://sandbox.invalid:44300");
    await card.getByLabel("SAP user (read-only)").fill("RFREAD");
    await card.getByLabel("Password environment variable (name)").fill("NOT_SET_ANYWHERE");
    await card.getByLabel("This is a sandbox or a copy, and the SAP user is read-only").check();
    await card.getByRole("button", { name: "Run smoke test" }).click();
    await expect(page.getByRole("alert").filter({ hasText: /NOT_SET_ANYWHERE/ }).first()).toBeVisible();
    await expect(card.getByText("Smoke test result")).toHaveCount(0);
    // a role without system:write does not even get the form
    await app.as("tina.tester");
    await expect(page.getByRole("heading", { name: "Connect a real system (read-only)" })).toHaveCount(0);
    await app.as("refresh.copilot");
    await expect(page.getByRole("heading", { name: "Connect a real system (read-only)" })).toHaveCount(0);
  } finally {
    await be.stop();
  }
});

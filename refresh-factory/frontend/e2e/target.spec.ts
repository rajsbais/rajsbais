import { expect, expectAccessible, startBackend, Keystone, test } from "./fixtures";

// Write access to a remote sandbox needs a request and a different approver. The loader here is a FAKE (host "target.invalid").
test("write access to a remote sandbox: requested by one person, approved by a different security officer, revocable at once", async ({ page }) => {
  const be = await startBackend({ RFACTORY_ALLOW_FAKE_ENDPOINTS: "1", E2E_SAP_PW: "x" });
  try {
    const app = new Keystone(page, be.url);
    await app.open();
    await app.loadLandscape();
    await app.nav("Landscape");
    const card = page.locator("section", { has: page.getByRole("heading", { name: "Connect a real system (read-only)" }) });
    await card.getByLabel("System ID (SID)").fill("EQ1");
    await card.getByLabel("Role").selectOption("QAS");
    await card.getByLabel("Client", { exact: true }).fill("200");
    await card.getByLabel("Application server host").fill("target.invalid");
    await card.getByLabel("SAP user (read-only)").fill("RFREAD");
    await card.getByLabel("Password environment variable (name)").fill("E2E_SAP_PW");
    await card.getByLabel("Calls per minute (throttle)").fill("60000");
    await card.getByLabel("This is a sandbox or a copy, and the SAP user is read-only").check();
    await card.getByRole("button", { name: "Run smoke test" }).click();
    await expect(card.getByText("Smoke test result")).toBeVisible();
    await card.getByRole("button", { name: "Register as read-only source" }).click();

    const box = page.locator(".write-box");
    await expect(box).toContainText(/Read-only/);
    await expectAccessible(page, "remote system, read-only");
    await box.getByLabel("Reason").fill("sandbox refresh test");
    await box.getByLabel(/outbound interfaces and jobs are inactive/).check();
    await box.getByRole("button", { name: "Request write access" }).click();
    await expect(box).toContainText(/waiting for a security officer/);
    await expect(box.getByRole("button", { name: "Approve write access" })).toBeDisabled(); // alice has no target:approve

    await app.as("sven.security");
    await page.getByRole("button", { name: /EQ1\/200/ }).last().click();
    await expect(box.getByRole("button", { name: "Approve write access" })).toBeEnabled();
    await box.getByRole("button", { name: "Approve write access" }).click();
    await expect(box).toContainText(/writable target/);
    await expect(box).toContainText(/attested inactive by sven.security/);
    await expectAccessible(page, "remote system, writable");
    const systems = (await app.api("alice.basis").get("/api/systems")).body as { sid: string; writable_target: boolean; adapter: string }[];
    expect(systems.find((s) => s.sid === "EQ1" && s.adapter === "rfc")?.writable_target).toBe(true);

    await box.getByRole("button", { name: "Revoke write access" }).click();
    await expect(box).toContainText(/Read-only/);
    const after = (await app.api("alice.basis").get("/api/systems")).body as { sid: string; writable_target: boolean; adapter: string }[];
    expect(after.find((s) => s.sid === "EQ1" && s.adapter === "rfc")?.writable_target).toBe(false);
  } finally {
    await be.stop();
  }
});

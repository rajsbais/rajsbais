import { expect, expectAccessible, test } from "./fixtures";

test("selective refresh: design, approve with separation of duties, execute, reconcile", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Selective designer");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  // production is never offered as a target
  await expect(page.getByLabel(/^Target/).locator("option", { hasText: "(PRD)" })).toHaveCount(0);
  await app.click("Create project");
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await expect(page.getByText(/objects/i).first()).toBeVisible();
  await expectAccessible(page, "designer: plan preview");

  await page.getByRole("tab", { name: "Conflicts" }).click();
  await app.click("Analyze conflicts");
  await page.getByLabel(/exists in the target/).selectOption("SKIP");
  await app.click("Apply policy and re-analyze");
  await page.getByRole("tab", { name: "Masking" }).click();
  await app.click(/Apply advisor/);
  await page.getByRole("tab", { name: "Approval" }).click();
  await app.click("Submit for approval");

  // the person who prepared the plan cannot approve it
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeDisabled();
  await app.as("carol.approver");
  await approve.click(); await app.settle();
  await app.as("alice.basis");
  await page.getByRole("tab", { name: "Execute" }).click();
  await app.click("Execute selective refresh");
  await expect(page.getByText("RELEASED").first()).toBeVisible();
  await expectAccessible(page, "reconciliation result");

  const audit = await app.api("erin.auditor").get("/api/audit/verify");
  expect(audit.body.valid).toBe(true);
  expect(app.errors).toEqual([]);
});

test("delta refresh: standing approval, first copy, then only the changes", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Delta refresh");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD) · ECC" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click(/Create weekend QA sync/);
  await app.click("Check masking coverage");
  await app.as("dave.privacy");
  await app.click(/recommended rule/);
  await app.as("alice.basis");
  await app.click("Submit for approval");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await app.click("Run now");
  await expect(page.getByText(/COMPLETED|RELEASED/).first()).toBeVisible();
  await page.getByText("Demo and scheduler controls").click();
  await app.click("Simulate source activity");
  await app.click("Preview delta");
  await app.click("Run now");
  const scenarios = await app.api("erin.auditor").get("/api/delta/scenarios");
  expect(scenarios.body[0].runs).toBe(2);
  expect(scenarios.body[0].last_run.new + scenarios.body[0].last_run.changed).toBeGreaterThan(0);
  await expectAccessible(page, "delta refresh after two runs");
  expect(app.errors).toEqual([]);
});

test("test data catalog: policy approval, then a tester provisions and reserves data", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Test catalog");
  await page.getByLabel("Target system").selectOption({ label: "EQ1/200 (QAS) · ECC" });
  await app.settle();
  await app.click("Create default policy");
  await app.click("Submit policy");
  await app.as("carol.approver");
  await app.click("Approve policy");
  await app.as("alice.basis");
  await page.getByLabel("Business scenario").selectOption("md_materials");
  await page.getByLabel("Provisioning mode").selectOption("subset");
  await page.getByLabel("How many").fill("8");
  await page.getByLabel("Reserve for me").uncheck();
  await app.click("Request");
  await app.as("tina.tester");
  await page.getByLabel("Business scenario").selectOption("o2c_complete");
  await page.getByLabel("Provisioning mode").selectOption("subset");
  await page.getByLabel("How many").fill("2");
  await page.getByLabel("Test case id").fill("QA-101");
  await page.getByLabel("Test case title").fill("Create invoice");
  await page.getByLabel("Reserve for me").check();
  await app.click("Request");
  await expect(page.getByText("RESERVED").first()).toBeVisible();
  await expectAccessible(page, "test catalog with datasets");
  expect(app.errors).toEqual([]);
});

test("lean client: approved template, estimate, build, validated client appears", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Lean client");
  await page.locator(".tpl", { hasText: "Functional test client" }).getByRole("button", { name: "Create template" }).click();
  await app.settle();
  await app.click("Submit");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await page.getByLabel("Environment template", { exact: true }).selectOption({ index: 1 });
  await page.getByLabel("Host system").selectOption({ label: "EQ1 · QAS · ECC" });
  await app.click("Estimate");
  await page.getByLabel("New client number").fill("320");
  await app.click("Build client");
  await expect(page.getByText("READY").first()).toBeVisible();
  await expectAccessible(page, "lean client after a build");
  expect(app.errors).toEqual([]);
});

test("post-copy: profile before the copy, three role approvals, gate passes", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Post-copy");
  await page.getByLabel("Target system").selectOption({ label: "EQ1/200 (QAS) · ECC" });
  await app.settle();
  await app.click("Capture profile now");
  await app.click("Submit");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await app.click(/simulate a system copy/, false);
  await page.getByLabel("Approved profile for the run").selectOption({ index: 1 });
  await app.click("Plan run");
  // each approval only by its own role
  for (const [user, button] of [["bastian.lead", "Approve as Basis lead"], ["ingrid.integration", "Approve as Integration owner"], ["sven.security", "Approve as Security officer"]]) {
    await app.as("alice.basis");
    await expect(page.getByRole("button", { name: button })).toBeDisabled();
    await app.as(user);
    await app.click(button);
  }
  await app.as("alice.basis");
  await app.click("Execute");
  await expect(page.getByText("COMPLETED").first()).toBeVisible();
  await expectAccessible(page, "post-copy after execution");
  expect(app.errors).toEqual([]);
});

test("full refresh: unmasked-data warning appears after the copy and is gone after release", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Post-copy");
  await page.getByLabel("Target system").selectOption({ label: "EQ1/200 (QAS) · ECC" });
  await app.settle();
  await app.click("Capture profile now");
  await app.click("Submit");
  await app.as("carol.approver");
  await app.click("Approve");
  await app.as("alice.basis");
  await app.nav("Full refresh");
  await page.getByLabel("Target system").selectOption({ label: "EQ1/200 (QAS) · ECC" });
  await app.settle();
  await page.getByLabel("Pre-copy profile").selectOption({ index: 1 });
  await app.click("Create program");
  await app.as("carol.approver");
  await app.click("Approve as change approver");
  await app.as("bastian.lead");
  await app.click("Confirm backup (Basis lead)");
  await app.as("alice.basis");
  await app.click("Run");
  await expect(page.getByText("target holds UNMASKED data")).toBeVisible();
  await expectAccessible(page, "full refresh waiting on post-copy approvals");
  await app.nav("Post-copy");
  for (const [user, button] of [["bastian.lead", "Approve as Basis lead"], ["ingrid.integration", "Approve as Integration owner"], ["sven.security", "Approve as Security officer"]]) {
    await app.as(user);
    await app.click(button);
  }
  await app.as("alice.basis");
  await app.nav("Full refresh");
  await app.click("Continue");
  await expect(page.getByText("target holds UNMASKED data")).toHaveCount(0);
  await app.as("sven.security");
  await app.click("Security sign-off");
  await app.as("alice.basis");
  await app.click("Continue");
  // the executor cannot release; an approver can
  await expect(page.getByRole("button", { name: "Release environment" })).toBeDisabled();
  await app.as("carol.approver");
  await app.click("Release environment");
  await expect(page.getByText("RELEASED", { exact: true })).toBeVisible();
  expect(app.errors).toEqual([]);
});

test("AI agents: an agent principal cannot apply; a human can; the copilot says it is a router", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("AI agents");
  await expect(page.getByText(/not a chat model/)).toBeVisible();
  await app.click("Run agent");
  await app.as("refresh.copilot");
  await app.click("Run agent");
  await expect(page.getByRole("button", { name: "Apply" }).first()).toBeDisabled();
  await expect(page.getByText(/agents and services never can/).first()).toBeVisible();
  await app.as("alice.basis");
  await app.click("Run agent");
  await app.click("Apply");
  await expect(page.getByText("APPLIED").first()).toBeVisible();
  await page.getByLabel("Ask the copilot").fill("are we compliant?");
  await app.click("Ask");
  await expect(page.getByText("Audit chain intact")).toBeVisible();
  await expectAccessible(page, "AI agents with a compliance report");
  expect(app.errors).toEqual([]);
});

test("orchestration: pipeline, human gate completed by a different person, window freeze, schedule approval", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.nav("Orchestration");
  await app.click("Submit demo pipeline");
  await app.click("Run scheduler tick");
  await expect(page.getByText("WAITING_HUMAN")).toBeVisible();
  await app.as("alice.basis");
  await app.as("carol.approver");
  await app.click("Complete gate");
  await app.as("alice.basis");
  await app.click("Run scheduler tick");
  await expect(page.getByText("WAITING_HUMAN")).toHaveCount(0);
  await app.click("Add 12h freeze");
  await expect(page.getByText("closed", { exact: true })).toBeVisible();
  await app.click("New nightly schedule");
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toBeDisabled(); // alice created it
  await app.as("carol.approver");
  await app.click("Approve");
  await expect(page.getByText("active", { exact: true })).toBeVisible();
  await expectAccessible(page, "orchestration with jobs, window and schedule");
  expect(app.errors).toEqual([]);
});

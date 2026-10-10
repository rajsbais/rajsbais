import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, expectAccessible, Keystone, startBackend, test } from "./fixtures";

// ---------------------------------------------------------------- scoped roles (ABAC), demo authentication
test("a regionally scoped steward sees only their systems and is refused other companies in the UI", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.as("cora.regional");
  await expect(page.getByTestId("scope")).toHaveText(/company 2000 · EP1, EQ1/);
  await app.nav("Landscape");
  await expect(page.getByText("EQ1/200").first()).toBeVisible();
  await expect(page.getByText("S4P/100")).toHaveCount(0);
  await expect(page.getByText("S4Q/200")).toHaveCount(0);
  await app.nav("Selective designer");
  await expect(page.getByLabel(/^Source/).locator("option", { hasText: "S4P" })).toHaveCount(0);
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  // the designer's default scope is company 1000: outside this person's scope
  await app.click(/Save manifest/);
  await expect(page.getByRole("alert")).toContainText(/outside your scope/);
  await page.getByLabel("Company codes (comma separated)").fill("2000");
  await app.click(/Save manifest/);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expectAccessible(page, "scoped steward: designer");
  // another person's S/4 project is not visible to them at all
  const alice = app.api("alice.basis");
  const systems = (await alice.get("/api/systems")).body as { id: string; sid: string }[];
  const made = await alice.post("/api/projects", { name: "S/4 refresh", source_id: systems.find((s) => s.sid === "S4P")!.id, target_id: systems.find((s) => s.sid === "S4Q")!.id });
  expect(made.status).toBe(201);
  await app.as("alice.basis");
  expect(await page.getByLabel("Active refresh project").locator("option").allInnerTexts()).toEqual(expect.arrayContaining([expect.stringContaining("S/4 refresh")]));
  await app.as("cora.regional");
  const projects = await page.getByLabel("Active refresh project").locator("option").allInnerTexts();
  expect(projects.join("|")).not.toContain("S/4 refresh");
  expect(app.errors.filter((e) => !e.includes("403"))).toEqual([]);
});

test("the audit view shows signed-chain status and offers the signed head for escrow", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.as("erin.auditor");
  await app.nav("Audit");
  await expect(page.getByText("chain and signatures valid")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download signed head" }).click();
  const d = await download;
  expect(d.suggestedFilename()).toBe("audit-head.json");
  await expectAccessible(page, "audit with signed chain");
});

// ---------------------------------------------------------------- OIDC mode
const b64 = (b: Buffer | string) => Buffer.from(b).toString("base64url");

function identityProvider() {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "e2e-1", use: "sig", alg: "RS256" };
  const dir = mkdtempSync(path.join(tmpdir(), "keystone-oidc-"));
  const jwks = path.join(dir, "jwks.json");
  writeFileSync(jwks, JSON.stringify({ keys: [jwk] }));
  const mint = (claims: Record<string, unknown>, key = privateKey) => {
    const now = Math.floor(Date.now() / 1000);
    const head = b64(JSON.stringify({ alg: "RS256", kid: "e2e-1", typ: "JWT" }));
    const body = b64(JSON.stringify({ iss: "https://idp.e2e.test", aud: "keystone", iat: now, exp: now + 300, ...claims }));
    const sig = sign("RSA-SHA256", Buffer.from(`${head}.${body}`), key);
    return `${head}.${body}.${b64(sig)}`;
  };
  return { jwks, mint, dir };
}

test("OIDC mode: no demo user switcher, token sign-in, scope shown, sign-out, bad tokens explained", async ({ page }) => {
  const idp = identityProvider();
  const be = await startBackend({ RFACTORY_AUTH: "oidc", RFACTORY_OIDC_ISSUER: "https://idp.e2e.test", RFACTORY_OIDC_AUDIENCE: "keystone", RFACTORY_OIDC_JWKS_FILE: idp.jwks });
  try {
    const app = new Keystone(page, be.url);
    await page.goto(be.url);
    await expect(page.getByLabel("Bearer token (OIDC)")).toBeVisible();
    await expect(page.locator("#user")).toHaveCount(0); // the demo switcher does not exist in this mode
    await expectAccessible(page, "sign-in form");

    await page.getByLabel("Bearer token (OIDC)").fill(idp.mint({ sub: "old", exp: Math.floor(Date.now() / 1000) - 3600, roles: ["basis"] }));
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText(/expired/);

    await page.getByLabel("Bearer token (OIDC)").fill(idp.mint({ sub: "reg-1", name: "Rita Regional", roles: ["data_steward", "basis"], rf_company_codes: ["2000"], rf_systems: ["EP1", "EQ1"] }));
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Rita Regional")).toBeVisible();
    await expect(page.getByTestId("scope")).toHaveText(/company 2000 · EP1, EQ1/);
    expect(await page.evaluate(() => localStorage.getItem("rf.token"))).toBeNull(); // never in localStorage
    expect(await page.evaluate(() => sessionStorage.getItem("rf.token"))).toBeTruthy();
    await app.click("Load synthetic landscape");
    await app.nav("Landscape");
    await expect(page.getByText("S4P/100")).toHaveCount(0); // ABAC flows from the token's claims
    await expect(page.getByText("EQ1/200").first()).toBeVisible();
    await expectAccessible(page, "oidc session");

    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect(page.getByLabel("Bearer token (OIDC)")).toBeVisible();
    expect(await page.evaluate(() => sessionStorage.getItem("rf.token"))).toBeNull();

    // a token for a different audience is refused with a reason, and the platform stays closed
    await page.getByLabel("Bearer token (OIDC)").fill(idp.mint({ sub: "x", aud: "other-app", roles: ["admin"] }));
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toContainText(/audience/);
    const r = await fetch(`${be.url}/api/systems`, { headers: { "X-Demo-User": "root.admin" } });
    expect(r.status).toBe(401); // the demo header is dead in this mode
  } finally {
    await be.stop();
  }
});

test("a plant-scoped steward is refused plants and plans outside their scope, and the discovery lists are filtered", async ({ app }) => {
  const { page } = app;
  await app.loadLandscape();
  await app.as("pete.plant");
  await expect(page.getByTestId("scope")).toHaveText(/all companies · EP1, EQ1 · plants 1000 · sales orgs 1000/);
  await app.nav("Selective designer");
  await page.getByLabel(/^Source/).selectOption({ label: "EP1/100 (PRD)" });
  await page.getByLabel(/^Target/).selectOption({ label: "EQ1/200 (QAS)" });
  await app.click("Create project");
  await page.getByLabel("Plants (comma separated, optional)").fill("1010");
  await app.click(/Save manifest/);
  await expect(page.getByRole("alert")).toContainText(/plants \['1010'\]/);
  await page.getByLabel("Plants (comma separated, optional)").fill("1000");
  await app.click(/Save manifest/);
  await app.click("Build plan");
  await expect(page.getByRole("alert")).toContainText(/contain plant\(s\) \['1010'\]/); // sales orders in the demo data mix plants: refused, never partially copied
  await expectAccessible(page, "plant-scoped steward: refused plan");
  const src = ((await app.api("alice.basis").get("/api/systems")).body as { id: string; sid: string }[]).find((s) => s.sid === "EP1")!;
  const d = (await app.api("pete.plant").get(`/api/systems/${src.id}/discovery`)).body as { plants: { plant: string }[] };
  expect(d.plants.map((p) => p.plant)).toEqual(["1000"]);
});

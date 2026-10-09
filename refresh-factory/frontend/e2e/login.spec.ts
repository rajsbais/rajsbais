import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import http from "node:http";
import { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, expectAccessible, Keystone, startBackend, test } from "./fixtures";

// A fake identity provider that enforces what a real one must: S256 PKCE, single-use codes, exact redirect URI and client id.
const b64 = (b: Buffer | string) => Buffer.from(b).toString("base64url");

async function fakeIdp(opts: { lifetime?: number } = {}) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "e2e-1", use: "sig", alg: "RS256" };
  const dir = mkdtempSync(path.join(tmpdir(), "keystone-login-"));
  const jwks = path.join(dir, "jwks.json");
  writeFileSync(jwks, JSON.stringify({ keys: [jwk] }));
  const mint = (claims: Record<string, unknown>) => {
    const now = Math.floor(Date.now() / 1000);
    const head = b64(JSON.stringify({ alg: "RS256", kid: "e2e-1", typ: "JWT" }));
    const body = b64(JSON.stringify({ iss: "https://idp.e2e.test", aud: "keystone", iat: now, exp: now + (opts.lifetime ?? 300), jti: Math.random().toString(36).slice(2), ...claims }));
    return `${head}.${body}.${b64(sign("RSA-SHA256", Buffer.from(`${head}.${body}`), privateKey))}`;
  };
  const st = { mode: "ok" as "ok" | "tamper" | "deny", authorize: [] as URLSearchParams[], token: [] as URLSearchParams[], logout: 0, redirectUri: "", codes: new Map<string, { challenge: string; nonce: string; used: boolean }>() };
  const server = http.createServer((req, res) => {
    const u = new URL(req.url!, "http://x");
    if (u.pathname === "/authorize") {
      st.authorize.push(u.searchParams);
      const q = u.searchParams;
      if (q.get("response_type") !== "code" || q.get("client_id") !== "keystone-ui" || q.get("redirect_uri") !== st.redirectUri || q.get("code_challenge_method") !== "S256" || !q.get("code_challenge") || !q.get("state")) {
        res.writeHead(400).end("invalid_request");
        return;
      }
      const back = new URL(q.get("redirect_uri")!);
      if (st.mode === "deny") { back.searchParams.set("error", "access_denied"); back.searchParams.set("error_description", "The user declined"); back.searchParams.set("state", q.get("state")!); }
      else {
        const code = b64(Buffer.from(String(Math.random())));
        st.codes.set(code, { challenge: q.get("code_challenge")!, nonce: q.get("nonce") ?? "", used: false });
        back.searchParams.set("code", code);
        back.searchParams.set("state", st.mode === "tamper" ? "forged-state" : q.get("state")!);
      }
      res.writeHead(302, { Location: back.toString() }).end();
    } else if (u.pathname === "/token" && req.method === "POST") {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        const f = new URLSearchParams(raw);
        st.token.push(f);
        const send = (code: number, body: object) => res.writeHead(code, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }).end(JSON.stringify(body));
        const c = st.codes.get(f.get("code") ?? "");
        if (!c || c.used) return send(400, { error: "invalid_grant" });
        c.used = true;
        if (f.get("redirect_uri") !== st.redirectUri || f.get("client_id") !== "keystone-ui" || f.has("client_secret")) return send(400, { error: "invalid_request" });
        if (createHash("sha256").update(f.get("code_verifier") ?? "").digest("base64url") !== c.challenge) return send(400, { error: "invalid_grant" });
        send(200, {
          token_type: "Bearer", expires_in: opts.lifetime ?? 300,
          access_token: mint({ sub: "lw-1", name: "Lena Login", roles: ["data_steward", "basis"], rf_company_codes: ["2000"], rf_systems: ["EP1", "EQ1"] }),
          id_token: mint({ sub: "lw-1", nonce: c.nonce }),
        });
      });
    } else if (u.pathname === "/logout") {
      st.logout++;
      res.writeHead(302, { Location: u.searchParams.get("post_logout_redirect_uri") ?? "/" }).end();
    } else res.writeHead(404).end();
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  return { url, jwks, st, close: () => new Promise<void>((r) => server.close(() => r())) };
}

async function withLogin(opts: { lifetime?: number }, fn: (ctx: { page: import("@playwright/test").Page; app: Keystone; idp: Awaited<ReturnType<typeof fakeIdp>>; be: { url: string } }) => Promise<void>, page: import("@playwright/test").Page) {
  const idp = await fakeIdp(opts);
  const be = await startBackend({
    RFACTORY_AUTH: "oidc", RFACTORY_OIDC_ISSUER: "https://idp.e2e.test", RFACTORY_OIDC_AUDIENCE: "keystone", RFACTORY_OIDC_JWKS_FILE: idp.jwks,
    RFACTORY_OIDC_AUTHORIZE_URL: `${idp.url}/authorize`, RFACTORY_OIDC_TOKEN_URL: `${idp.url}/token`, RFACTORY_OIDC_CLIENT_ID: "keystone-ui",
    RFACTORY_OIDC_END_SESSION_URL: `${idp.url}/logout`,
  });
  idp.st.redirectUri = `${be.url}/`;
  try { await fn({ page, app: new Keystone(page, be.url), idp, be }); } finally { await be.stop(); await idp.close(); }
}

test("browser login with Authorization Code + PKCE: signed in, scoped, nothing secret left in the URL, signed out at the IdP", async ({ page }) => {
  await withLogin({}, async ({ page, app, idp, be }) => {
    await page.goto(be.url);
    await expect(page.getByRole("button", { name: "Sign in with your identity provider" })).toBeVisible();
    await expectAccessible(page, "sign-in with identity provider");
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByText("Lena Login")).toBeVisible();
    await expect(page.getByTestId("scope")).toHaveText(/company 2000 · EP1, EQ1/);
    expect(page.url()).toBe(`${be.url}/`); // no code, no state in the address bar
    const a = idp.st.authorize[0];
    expect(a.get("code_challenge_method")).toBe("S256");
    expect(a.get("code_challenge")!.length).toBeGreaterThanOrEqual(43);
    expect(a.get("client_secret")).toBeNull();
    const t = idp.st.token[0];
    expect(createHash("sha256").update(t.get("code_verifier")!).digest("base64url")).toBe(a.get("code_challenge"));
    expect(t.get("code_verifier")!.length).toBeGreaterThanOrEqual(43);
    expect(await page.evaluate(() => JSON.stringify({ ...localStorage }).match(/token|session|pkce|eyJ/i))).toBeNull(); // no credential in localStorage
    expect(await page.evaluate(() => sessionStorage.getItem("rf.pkce"))).toBeNull(); // the one-time record is spent
    await app.click("Load synthetic landscape");
    await app.nav("Landscape");
    await expect(page.getByText("S4P/100")).toHaveCount(0);
    await expectAccessible(page, "signed in through the identity provider");

    await page.reload(); // the session survives a reload in the same tab
    await expect(page.getByText("Lena Login")).toBeVisible();
    expect(idp.st.token).toHaveLength(1); // and no second exchange happened

    const issued = await page.evaluate(() => sessionStorage.getItem("rf.token"));
    expect((await fetch(`${be.url}/api/me`, { headers: { Authorization: `Bearer ${issued}` } })).status).toBe(200);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await expect.poll(() => idp.st.logout).toBe(1); // the browser is sent to the identity provider's logout and back
    await page.waitForLoadState("load");
    await expect(page.getByRole("button", { name: "Sign in with your identity provider" })).toBeVisible();
    const dead = await fetch(`${be.url}/api/me`, { headers: { Authorization: `Bearer ${issued}` } }); // a copy of the token no longer works
    expect(dead.status).toBe(401);
    expect(JSON.stringify(await dead.json())).toContain("revoked");
    expect(await page.evaluate(() => sessionStorage.getItem("rf.session"))).toBeNull();
    expect(await page.evaluate(() => sessionStorage.getItem("rf.token"))).toBeNull();

    // a second login uses a fresh verifier and challenge (the browser's randomness is real)
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByText("Lena Login")).toBeVisible();
    expect(idp.st.authorize[1].get("code_challenge")).not.toBe(a.get("code_challenge"));
    expect(idp.st.authorize[1].get("state")).not.toBe(a.get("state"));
  }, page);
});

test("a callback with a different state is refused and the token endpoint is never called", async ({ page }) => {
  await withLogin({}, async ({ page, idp, be }) => {
    idp.st.mode = "tamper";
    await page.goto(be.url);
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByRole("alert").filter({ hasText: /state mismatch/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sign in with your identity provider" })).toBeVisible();
    expect(idp.st.token).toHaveLength(0);
    expect(page.url()).toBe(`${be.url}/`);
    expect(await page.evaluate(() => sessionStorage.getItem("rf.token"))).toBeNull();
  }, page);
});

test("a refusal at the identity provider is explained, and a replayed callback URL does nothing", async ({ page }) => {
  await withLogin({}, async ({ page, idp, be }) => {
    idp.st.mode = "deny";
    await page.goto(be.url);
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByRole("alert").filter({ hasText: /The user declined/ })).toBeVisible();

    idp.st.mode = "ok";
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByText("Lena Login")).toBeVisible();
    const used = new URL(idp.st.authorize[1].get("redirect_uri")!);
    used.searchParams.set("code", [...idp.st.codes.keys()][0]);
    used.searchParams.set("state", idp.st.authorize[1].get("state")!);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await page.goto(used.toString()); // replay the spent callback in a fresh sign-in state
    await expect(page.getByRole("alert").filter({ hasText: /not started in this browser tab/ })).toBeVisible();
    expect(idp.st.token).toHaveLength(1);
    expect(page.url()).toBe(`${be.url}/`);
  }, page);
});

test("when the access token expires the session ends and the person is asked to sign in again", async ({ page }) => {
  await withLogin({ lifetime: 4 }, async ({ page, idp, be }) => {
    await page.goto(be.url);
    await page.getByRole("button", { name: "Sign in with your identity provider" }).click();
    await expect(page.getByText("Lena Login")).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: /session expired/ })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("button", { name: "Sign in with your identity provider" })).toBeVisible();
    expect(await page.evaluate(() => sessionStorage.getItem("rf.token"))).toBeNull();
  }, page);
});

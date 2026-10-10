import { createHash, randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";
import { b64url, beginLogin, completeLogin, Env, loadSession, logoutUrl, PENDING_KEY, SESSION_KEY } from "../src/pkce";

// Unit tests of the PKCE module: pure functions with injected storage, clock, randomness and fetch (no browser involved).
const cfg = { authorize_url: "https://idp.test/authorize", token_url: "https://idp.test/token", client_id: "keystone-ui", scope: "openid profile", end_session_url: "https://idp.test/logout" };
const REDIRECT = "https://app.test/";

function env(over: Partial<Env> & { responses?: { status: number; body: unknown }[] } = {}) {
  const mem = new Map<string, string>();
  const calls: { url: string; init: RequestInit }[] = [];
  const responses = over.responses ?? [];
  const e: Env & { mem: Map<string, string>; calls: typeof calls; t: { now: number } } = {
    mem, calls, t: { now: 1_000_000 },
    storage: { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => void mem.set(k, v), removeItem: (k) => void mem.delete(k) },
    fetch: (async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      const r = responses.shift() ?? { status: 500, body: {} };
      return { ok: r.status < 400, status: r.status, json: async () => r.body } as Response;
    }) as unknown as typeof fetch,
    now: () => e.t.now,
    random: (n) => new Uint8Array(randomBytes(n)),
    sha256: async (s) => new Uint8Array(createHash("sha256").update(s).digest()),
    ...over,
  };
  return e;
}
const jwt = (claims: object) => `x.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.y`;
const pend = (e: ReturnType<typeof env>) => JSON.parse(e.mem.get(PENDING_KEY)!);

test("the challenge is the S256 hash of the verifier (RFC 7636 appendix B vector)", async () => {
  const e = env();
  const verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
  expect(b64url(await e.sha256(verifier))).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
});

test("begin: S256 only, long random verifier kept local, state and nonce set, nothing secret in the URL", async () => {
  const e = env();
  const url = new URL(await beginLogin(cfg, REDIRECT, e));
  const q = url.searchParams;
  const p = pend(e);
  expect(q.get("code_challenge_method")).toBe("S256");
  expect(q.get("response_type")).toBe("code");
  expect(q.get("client_id")).toBe("keystone-ui");
  expect(q.get("redirect_uri")).toBe(REDIRECT);
  expect(q.get("scope")).toBe("openid profile");
  expect(p.verifier).toMatch(/^[A-Za-z0-9_-]{86}$/);
  expect(q.get("code_challenge")).toBe(createHash("sha256").update(p.verifier).digest("base64url"));
  expect(url.toString()).not.toContain(p.verifier);
  expect([q.get("state"), q.get("nonce")]).toEqual([p.state, p.nonce]);
  expect(new Set([p.state, p.nonce, p.verifier]).size).toBe(3);
  const a = env(), b = env();
  await beginLogin(cfg, REDIRECT, a); await beginLogin(cfg, REDIRECT, b);
  expect(pend(a).verifier).not.toBe(pend(b).verifier);
});

test("no callback parameters: nothing happens", async () => {
  const e = env();
  expect((await completeLogin(cfg, "?foo=1", e)).kind).toBe("none");
  expect(e.calls).toHaveLength(0);
});

test("a state that does not match is refused without contacting the token endpoint, and the pending request is spent", async () => {
  const e = env();
  await beginLogin(cfg, REDIRECT, e);
  const out = await completeLogin(cfg, "?code=abc&state=forged", e);
  expect(out).toMatchObject({ kind: "error", clean: true });
  expect((out as { message: string }).message).toMatch(/state mismatch/);
  expect(e.calls).toHaveLength(0);
  expect(e.mem.has(PENDING_KEY)).toBe(false);
  expect(e.mem.has(SESSION_KEY)).toBe(false);
});

test("a callback nobody asked for, or a missing state, is refused", async () => {
  const e = env();
  expect(((await completeLogin(cfg, "?code=abc&state=x", e)) as { message: string }).message).toMatch(/not started in this browser tab/);
  await beginLogin(cfg, REDIRECT, e);
  expect(((await completeLogin(cfg, "?code=abc", e)) as { message: string }).message).toMatch(/state mismatch/);
  expect(e.calls).toHaveLength(0);
});

test("a login older than ten minutes expires", async () => {
  const e = env();
  await beginLogin(cfg, REDIRECT, e);
  const s = pend(e).state;
  e.t.now += 10 * 60 * 1000 + 1;
  expect(((await completeLogin(cfg, `?code=abc&state=${s}`, e)) as { message: string }).message).toMatch(/expired/);
  expect(e.calls).toHaveLength(0);
});

test("an error from the identity provider is shown and spends the request", async () => {
  const e = env();
  await beginLogin(cfg, REDIRECT, e);
  const out = await completeLogin(cfg, "?error=access_denied&error_description=User%20said%20no", e);
  expect(out).toMatchObject({ kind: "error" });
  expect((out as { message: string }).message).toContain("User said no");
  expect(e.mem.has(PENDING_KEY)).toBe(false);
});

test("success: the code is exchanged once with the verifier, the session is stored, and a replay is refused", async () => {
  const e = env({ responses: [{ status: 200, body: { access_token: "AT", token_type: "Bearer", expires_in: 300 } }] });
  await beginLogin(cfg, REDIRECT, e);
  const p = pend(e);
  const out = await completeLogin(cfg, `?code=the-code&state=${p.state}`, e);
  expect(out).toMatchObject({ kind: "ok", session: { access_token: "AT", expires_at: e.t.now + 300_000 } });
  expect(e.calls).toHaveLength(1);
  expect(e.calls[0].url).toBe(cfg.token_url);
  const form = new URLSearchParams(e.calls[0].init.body as string);
  expect(Object.fromEntries(form)).toEqual({ grant_type: "authorization_code", code: "the-code", redirect_uri: REDIRECT, client_id: "keystone-ui", code_verifier: p.verifier });
  expect(form.has("client_secret")).toBe(false);
  expect(e.mem.has(PENDING_KEY)).toBe(false);
  // the same callback again (reload, replay, back button): refused, no second exchange
  const again = await completeLogin(cfg, `?code=the-code&state=${p.state}`, e);
  expect(again.kind).toBe("error");
  expect(e.calls).toHaveLength(1);
});

test("the id token's nonce must be this request's nonce", async () => {
  const e = env();
  await beginLogin(cfg, REDIRECT, e);
  const p = pend(e);
  e.fetch = (async () => ({ ok: true, status: 200, json: async () => ({ access_token: "AT", token_type: "Bearer", expires_in: 60, id_token: jwt({ nonce: "someone-elses" }) }) })) as unknown as typeof fetch;
  const out = await completeLogin(cfg, `?code=c&state=${p.state}`, e);
  expect(out).toMatchObject({ kind: "error" });
  expect((out as { message: string }).message).toMatch(/nonce/);
  expect(e.mem.has(SESSION_KEY)).toBe(false);
  const f = env({ responses: [{ status: 200, body: { access_token: "AT", expires_in: 60, id_token: "" } }] });
  await beginLogin(cfg, REDIRECT, f);
  const ok = await completeLogin(cfg, `?code=c&state=${pend(f).state}`, f);
  expect(ok.kind).toBe("ok"); // no id token at all is allowed (plain OAuth); a WRONG one is not
  const g = env();
  await beginLogin(cfg, REDIRECT, g);
  const pg = pend(g);
  g.fetch = (async () => ({ ok: true, status: 200, json: async () => ({ access_token: "AT", expires_in: 60, id_token: jwt({ nonce: pg.nonce }) }) })) as unknown as typeof fetch;
  expect((await completeLogin(cfg, `?code=c&state=${pg.state}`, g)).kind).toBe("ok");
});

test("a refused or malformed token response never produces a session", async () => {
  for (const r of [{ status: 400, body: { error: "invalid_grant" } }, { status: 200, body: {} }, { status: 200, body: { access_token: "AT", token_type: "mac" } }, { status: 200, body: { access_token: 42 } }]) {
    const e = env({ responses: [r] });
    await beginLogin(cfg, REDIRECT, e);
    const out = await completeLogin(cfg, `?code=c&state=${pend(e).state}`, e);
    expect(out.kind, JSON.stringify(r)).toBe("error");
    expect(e.mem.has(SESSION_KEY)).toBe(false);
  }
  const e = env();
  await beginLogin(cfg, REDIRECT, e);
  const s = pend(e).state;
  e.fetch = (async () => { throw new Error("network"); }) as unknown as typeof fetch;
  expect(((await completeLogin(cfg, `?code=c&state=${s}`, e)) as { message: string }).message).toMatch(/could not be reached/);
});

test("expiry comes from expires_in, else from the token's own exp, else a five-minute default", async () => {
  const run = async (body: object) => {
    const e = env({ responses: [{ status: 200, body }] });
    await beginLogin(cfg, REDIRECT, e);
    return { e, out: await completeLogin(cfg, `?code=c&state=${pend(e).state}`, e) };
  };
  const a = await run({ access_token: jwt({ exp: 1_500 }) });
  expect((a.out as { session: { expires_at: number } }).session.expires_at).toBe(1_500_000);
  const b = await run({ access_token: "opaque" });
  expect((b.out as { session: { expires_at: number } }).session.expires_at).toBe(b.e.t.now + 300_000);
});

test("a stored session is dropped once it has expired; logout clears everything and builds the end-session URL", async () => {
  const e = env({ responses: [{ status: 200, body: { access_token: "AT", expires_in: 60, id_token: "" } }] });
  await beginLogin(cfg, REDIRECT, e);
  await completeLogin(cfg, `?code=c&state=${pend(e).state}`, e);
  expect(loadSession(e)?.access_token).toBe("AT");
  e.t.now += 61_000;
  expect(loadSession(e)).toBeNull();
  expect(e.mem.has(SESSION_KEY)).toBe(false);

  e.mem.set(SESSION_KEY, JSON.stringify({ access_token: "AT", expires_at: e.t.now + 1000, id_token: "idt" }));
  e.mem.set(PENDING_KEY, "{}");
  const u = new URL(logoutUrl(cfg, e, REDIRECT)!);
  expect(u.searchParams.get("id_token_hint")).toBe("idt");
  expect(u.searchParams.get("post_logout_redirect_uri")).toBe(REDIRECT);
  expect([e.mem.has(SESSION_KEY), e.mem.has(PENDING_KEY)]).toEqual([false, false]);
  expect(logoutUrl({ ...cfg, end_session_url: null }, e, REDIRECT)).toBeNull();
});

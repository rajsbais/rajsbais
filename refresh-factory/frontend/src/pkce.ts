/**
 * Authorization Code + PKCE (S256) for a PUBLIC client: the browser runs the whole flow and no client secret exists.
 *
 * What it guarantees
 *  - the code verifier is 64 random bytes, base64url (86 characters); only its SHA-256 challenge ever travels in the authorize URL;
 *    the method is always S256 (never "plain");
 *  - `state` binds the callback to this tab's request and `nonce` binds the id token to it; the pending record is single-use, expires
 *    after 10 minutes and is removed BEFORE the code is exchanged, so a reload or a replay cannot reuse it;
 *  - the code and state are removed from the address bar before anything else happens;
 *  - tokens are kept in sessionStorage only (they die with the tab) and are never logged or put in a URL.
 *
 * What it does not do: refresh tokens or silent renewal (the session ends when the access token expires); it does not verify the id
 * token's signature (the API verifies the access token it is given; the id token only supplies the logout hint and the nonce check, and
 * it arrives straight from the token endpoint over TLS); crypto.subtle needs a secure context (https or localhost).
 */
export interface LoginConfig { authorize_url: string; token_url: string; client_id: string; scope: string; end_session_url?: string | null }
export interface Pending { state: string; nonce: string; verifier: string; redirect_uri: string; created: number }
export interface Session { access_token: string; expires_at: number; id_token?: string }
export interface Env {
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  fetch: typeof fetch;
  now: () => number;
  random: (n: number) => Uint8Array;
  sha256: (s: string) => Promise<Uint8Array>;
}

export const PENDING_KEY = "rf.pkce";
export const SESSION_KEY = "rf.session";
export const MAX_AGE_MS = 10 * 60 * 1000;

export const b64url = (b: Uint8Array): string => btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export const browserEnv = (): Env => ({
  storage: sessionStorage,
  fetch: (...a) => fetch(...a),
  now: () => Date.now(),
  random: (n) => crypto.getRandomValues(new Uint8Array(n)),
  sha256: async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s))),
});

export async function beginLogin(cfg: LoginConfig, redirectUri: string, env: Env): Promise<string> {
  const verifier = b64url(env.random(64));
  const challenge = b64url(await env.sha256(verifier));
  const pending: Pending = { state: b64url(env.random(16)), nonce: b64url(env.random(16)), verifier, redirect_uri: redirectUri, created: env.now() };
  env.storage.setItem(PENDING_KEY, JSON.stringify(pending));
  const u = new URL(cfg.authorize_url);
  const q = u.searchParams;
  q.set("response_type", "code");
  q.set("client_id", cfg.client_id);
  q.set("redirect_uri", redirectUri);
  q.set("scope", cfg.scope);
  q.set("state", pending.state);
  q.set("nonce", pending.nonce);
  q.set("code_challenge", challenge);
  q.set("code_challenge_method", "S256");
  return u.toString();
}

export type Outcome = { kind: "none" } | { kind: "ok"; session: Session } | { kind: "error"; message: string };

function decodeClaims(jwt: string): Record<string, unknown> {
  try { return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0)))); } catch { return {}; }
}

/** Call once on page load with the current query string. Returns what happened; `clean` is the address-bar path to restore (no code, no state). */
export async function completeLogin(cfg: LoginConfig, search: string, env: Env): Promise<Outcome & { clean?: boolean }> {
  const q = new URLSearchParams(search);
  const code = q.get("code"), state = q.get("state"), err = q.get("error");
  if (!code && !err) return { kind: "none" };
  const raw = env.storage.getItem(PENDING_KEY);
  env.storage.removeItem(PENDING_KEY); // single use, whatever happens next
  const fail = (message: string): Outcome & { clean: boolean } => ({ kind: "error", message, clean: true });
  if (err) return fail(`The identity provider refused the sign-in: ${(q.get("error_description") || err).slice(0, 200)}`);
  let p: Pending | null = null;
  try { p = raw ? JSON.parse(raw) : null; } catch { p = null; }
  if (!p) return fail("This sign-in was not started in this browser tab (or was already used). Start again.");
  if (env.now() - p.created > MAX_AGE_MS) return fail("The sign-in took too long and expired. Start again.");
  if (!state || state !== p.state) return fail("The sign-in response does not belong to this request (state mismatch). Nothing was accepted.");
  let r: Response;
  try {
    r = await env.fetch(cfg.token_url, {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body: new URLSearchParams({ grant_type: "authorization_code", code: code!, redirect_uri: p.redirect_uri, client_id: cfg.client_id, code_verifier: p.verifier }).toString(),
    });
  } catch {
    return fail("The identity provider's token endpoint could not be reached.");
  }
  let body: Record<string, unknown> = {};
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) return fail(`The token request was refused (${String(body.error ?? r.status).slice(0, 60)}).`);
  const at = body.access_token;
  if (typeof at !== "string" || !at) return fail("The token response had no access token.");
  if (String(body.token_type ?? "Bearer").toLowerCase() !== "bearer") return fail("The token response is not a bearer token.");
  const idt = typeof body.id_token === "string" ? body.id_token : undefined;
  if (idt && decodeClaims(idt).nonce !== p.nonce) return fail("The id token does not carry this request's nonce. Nothing was accepted.");
  const exp = typeof body.expires_in === "number" ? env.now() + body.expires_in * 1000 : Number(decodeClaims(at).exp) * 1000 || env.now() + 5 * 60 * 1000;
  const session: Session = { access_token: at, expires_at: exp, ...(idt ? { id_token: idt } : {}) };
  env.storage.setItem(SESSION_KEY, JSON.stringify(session));
  return { kind: "ok", session, clean: true };
}

export function loadSession(env: Env): Session | null {
  try {
    const s: Session = JSON.parse(env.storage.getItem(SESSION_KEY) || "null");
    if (!s || typeof s.access_token !== "string" || env.now() >= s.expires_at) { env.storage.removeItem(SESSION_KEY); return null; }
    return s;
  } catch { return null; }
}

export function logoutUrl(cfg: LoginConfig, env: Env, redirectUri: string): string | null {
  const idt = (() => { try { return JSON.parse(env.storage.getItem(SESSION_KEY) || "null")?.id_token as string | undefined; } catch { return undefined; } })();
  env.storage.removeItem(SESSION_KEY);
  env.storage.removeItem(PENDING_KEY);
  if (!cfg.end_session_url) return null;
  const u = new URL(cfg.end_session_url);
  u.searchParams.set("client_id", cfg.client_id);
  u.searchParams.set("post_logout_redirect_uri", redirectUri);
  if (idt) u.searchParams.set("id_token_hint", idt);
  return u.toString();
}

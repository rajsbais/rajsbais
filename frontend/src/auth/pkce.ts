/**
 * Browser side of the OpenID Connect authorization code flow with PKCE (RFC 7636, S256).
 *
 * Pure functions only: no DOM, no storage. Works in browsers and in Node 20+ (globalThis.crypto), so the
 * maths is unit-tested against the RFC 7636 appendix B vector.
 */

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";

function cryptoApi(): Crypto {
  const c = (globalThis as any).crypto as Crypto | undefined;
  if (!c || !c.getRandomValues) throw new Error("Web Crypto is not available in this context");
  return c;
}

export function base64url(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Random string from the RFC 7636 "unreserved" alphabet; 43..128 characters. */
export function randomVerifier(length = 64): string {
  if (length < 43 || length > 128) throw new Error("code_verifier must be 43..128 characters");
  const bytes = new Uint8Array(length);
  cryptoApi().getRandomValues(bytes);
  let out = "";
  for (let i = 0; i < length; i++) out += ALPHABET[bytes[i] % ALPHABET.length];
  return out;
}

export function randomToken(bytes = 32): string {
  const b = new Uint8Array(bytes);
  cryptoApi().getRandomValues(b);
  return base64url(b);
}

/** S256 code challenge: BASE64URL(SHA256(ASCII(code_verifier))). Requires a secure context (https or localhost). */
export async function codeChallenge(verifier: string): Promise<string> {
  const subtle = cryptoApi().subtle;
  if (!subtle) throw new Error("PKCE needs crypto.subtle, which browsers expose only in a secure context (https:// or localhost)");
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

export interface OidcPublicConfig {
  enabled: boolean;
  issuer?: string;
  client_id?: string;
  scopes?: string;
  exchange?: "api" | "browser";
  authorization_endpoint?: string;
  token_endpoint?: string;
  end_session_endpoint?: string;
  pkce?: "S256";
  error?: string;
  dev_login?: boolean;
}

export interface PendingLogin {
  verifier: string;
  state: string;
  nonce: string;
  redirect_uri: string;
  return_to: string;
  started_at: number;
}

/** Build the authorization request. The pending login (verifier/state/nonce) must be kept by the caller. */
export async function buildAuthorizationRequest(cfg: OidcPublicConfig, redirectUri: string, returnTo = "/"): Promise<{ url: string; pending: PendingLogin }> {
  if (!cfg.enabled || !cfg.authorization_endpoint || !cfg.client_id) throw new Error(cfg.error || "OIDC login is not configured");
  const verifier = randomVerifier();
  const pending: PendingLogin = { verifier, state: randomToken(), nonce: randomToken(), redirect_uri: redirectUri, return_to: returnTo, started_at: Date.now() };
  const u = new URL(cfg.authorization_endpoint);
  u.searchParams.set("response_type", "code");
  u.searchParams.set("client_id", cfg.client_id);
  u.searchParams.set("redirect_uri", redirectUri);
  u.searchParams.set("scope", cfg.scopes || "openid profile email");
  u.searchParams.set("state", pending.state);
  u.searchParams.set("nonce", pending.nonce);
  u.searchParams.set("code_challenge", await codeChallenge(verifier));
  u.searchParams.set("code_challenge_method", "S256");
  return { url: u.toString(), pending };
}

export interface CallbackParams { code?: string; state?: string; error?: string; error_description?: string }

export function parseCallback(search: string): CallbackParams {
  const q = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const out: CallbackParams = {};
  for (const k of ["code", "state", "error", "error_description"] as const) { const v = q.get(k); if (v) out[k] = v; }
  return out;
}

/** Validate the provider's redirect against the pending login. Throws on CSRF (state) or provider errors. */
export function checkCallback(params: CallbackParams, pending: PendingLogin | null, maxAgeMs = 10 * 60 * 1000): { code: string; pending: PendingLogin } {
  if (params.error) throw new Error(`${params.error}${params.error_description ? `: ${params.error_description}` : ""}`);
  if (!pending) throw new Error("no login in progress in this browser tab (state missing)");
  if (Date.now() - pending.started_at > maxAgeMs) throw new Error("login attempt expired, start again");
  if (!params.state || params.state !== pending.state) throw new Error("state mismatch - the response does not belong to this login attempt");
  if (!params.code) throw new Error("authorization code missing from callback");
  return { code: params.code, pending };
}

/** Decode a JWT payload WITHOUT verifying it. Used only for display and for the client-side nonce check; the API verifies. */
export function unverifiedClaims(jwt: string | null | undefined): Record<string, any> {
  try {
    if (!jwt) return {};
    const b = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(b + "=".repeat((4 - (b.length % 4)) % 4)));
  } catch { return {}; }
}

/** Form body for a direct (browser-side) token request. Only used when the API reports exchange = "browser". */
export function tokenRequestBody(cfg: OidcPublicConfig, code: string, pending: PendingLogin): URLSearchParams {
  return new URLSearchParams({ grant_type: "authorization_code", code, code_verifier: pending.verifier, redirect_uri: pending.redirect_uri, client_id: cfg.client_id || "" });
}

export function endSessionUrl(cfg: OidcPublicConfig, idToken: string | null, postLogoutRedirect: string): string | null {
  if (!cfg.end_session_endpoint) return null;
  const u = new URL(cfg.end_session_endpoint);
  if (idToken) u.searchParams.set("id_token_hint", idToken);
  if (cfg.client_id) u.searchParams.set("client_id", cfg.client_id);
  u.searchParams.set("post_logout_redirect_uri", postLogoutRedirect);
  return u.toString();
}

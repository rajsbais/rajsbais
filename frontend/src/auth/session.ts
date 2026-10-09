/**
 * Browser session: token storage, OIDC login/callback/logout orchestration and silent refresh.
 * Dev HMAC login (username/password) and OIDC PKCE login share the same session shape.
 */
import { api, getToken, setSession, clearSession, sessionInfo, type SessionUser } from "../api";
import { buildAuthorizationRequest, checkCallback, endSessionUrl, parseCallback, tokenRequestBody, unverifiedClaims, type OidcPublicConfig, type PendingLogin } from "./pkce";

const PENDING_KEY = "sdtf.oidc.pending";
export const CALLBACK_PATH = "/auth/callback";

let configPromise: Promise<OidcPublicConfig> | null = null;

/** Provider configuration published by the API (no secrets). Cached per page load. */
export function oidcConfig(force = false): Promise<OidcPublicConfig> {
  if (!configPromise || force) configPromise = api<OidcPublicConfig>("/auth/oidc/config").catch((e) => ({ enabled: false, dev_login: true, error: e.message } as OidcPublicConfig));
  return configPromise;
}

export function redirectUri(): string { return `${window.location.origin}${CALLBACK_PATH}`; }

function savePending(p: PendingLogin | null) {
  try { if (p) sessionStorage.setItem(PENDING_KEY, JSON.stringify(p)); else sessionStorage.removeItem(PENDING_KEY); } catch {}
}
function loadPending(): PendingLogin | null {
  try { const s = sessionStorage.getItem(PENDING_KEY); return s ? JSON.parse(s) : null; } catch { return null; }
}

/** Start the authorization code + PKCE flow: remember verifier/state/nonce in this tab, then leave for the provider. */
export async function beginOidcLogin(returnTo = "/"): Promise<void> {
  const cfg = await oidcConfig();
  const { url, pending } = await buildAuthorizationRequest(cfg, redirectUri(), returnTo);
  savePending(pending);
  window.location.assign(url);
}

export interface LoginResult { access_token: string; token_kind: string; expires_at: number; id_token?: string | null; refresh_token?: string | null; username: string; roles: string[]; display_name: string; tenant_id: string }

/** Handle the provider redirect. Returns the path to continue to. Throws with a human-readable message. */
export async function completeOidcLogin(search: string): Promise<string> {
  const cfg = await oidcConfig();
  const { code, pending } = checkCallback(parseCallback(search), loadPending());
  savePending(null);
  let result: LoginResult;
  if (cfg.exchange === "browser") {
    if (!cfg.token_endpoint) throw new Error("token endpoint unknown");
    const res = await fetch(cfg.token_endpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" }, body: tokenRequestBody(cfg, code, pending) });
    const tokens = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`token endpoint rejected the code: ${tokens.error_description || tokens.error || res.status}`);
    if (unverifiedClaims(tokens.id_token).nonce !== pending.nonce) throw new Error("nonce mismatch");
    result = await api<LoginResult>("/auth/oidc/exchange", { body: { tokens, nonce: pending.nonce } });
  } else {
    result = await api<LoginResult>("/auth/oidc/exchange", { body: { code, code_verifier: pending.verifier, redirect_uri: pending.redirect_uri, nonce: pending.nonce } });
  }
  if (unverifiedClaims(result.id_token).nonce && unverifiedClaims(result.id_token).nonce !== pending.nonce) throw new Error("nonce mismatch");
  applyLogin(result);
  return pending.return_to || "/";
}

export function applyLogin(r: LoginResult) {
  const user: SessionUser = { username: r.username, roles: r.roles, display_name: r.display_name, tenant_id: r.tenant_id, method: "oidc" };
  setSession(r.access_token, user, { expires_at: r.expires_at, id_token: r.id_token || null, refresh_token: r.refresh_token || null });
  scheduleRefresh();
}

let refreshTimer: number | null = null;

/** Refresh ~60s before the bearer expires when the provider issued a refresh token; otherwise let it lapse and re-login. */
export function scheduleRefresh() {
  if (refreshTimer) { window.clearTimeout(refreshTimer); refreshTimer = null; }
  const s = sessionInfo();
  if (!s || s.user?.method !== "oidc" || !s.expires_at || !s.refresh_token) return;
  const delay = Math.max(5_000, s.expires_at * 1000 - Date.now() - 60_000);
  refreshTimer = window.setTimeout(async () => {
    try {
      const r = await api<LoginResult>("/auth/oidc/refresh", { body: { refresh_token: s.refresh_token } });
      applyLogin(r);
    } catch { clearSession(); window.dispatchEvent(new Event("sdtf.auth")); }
  }, delay);
}

/** Local sign-out, then RP-initiated logout at the provider when it offers one. */
export async function signOut(): Promise<void> {
  const s = sessionInfo();
  clearSession();
  if (refreshTimer) { window.clearTimeout(refreshTimer); refreshTimer = null; }
  if (s?.user?.method === "oidc") {
    const cfg = await oidcConfig();
    const url = endSessionUrl(cfg, s.id_token, window.location.origin + "/");
    if (url) { window.location.assign(url); return; }
  }
  window.dispatchEvent(new Event("sdtf.auth"));
}

export function isAuthenticated(): boolean {
  const s = sessionInfo();
  if (!getToken()) return false;
  if (s?.expires_at && s.expires_at * 1000 < Date.now()) { clearSession(); return false; }
  return true;
}

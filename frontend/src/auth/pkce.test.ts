import { describe, expect, it } from "vitest";
import { base64url, buildAuthorizationRequest, checkCallback, codeChallenge, endSessionUrl, parseCallback, randomVerifier, tokenRequestBody, unverifiedClaims } from "./pkce";

const CFG = { enabled: true, issuer: "https://idp.example.com/realms/sdtf", client_id: "sdtf-ui", scopes: "openid profile", exchange: "api" as const, authorization_endpoint: "https://idp.example.com/realms/sdtf/protocol/openid-connect/auth", token_endpoint: "https://idp.example.com/realms/sdtf/protocol/openid-connect/token", end_session_endpoint: "https://idp.example.com/realms/sdtf/protocol/openid-connect/logout", pkce: "S256" as const };

describe("PKCE (RFC 7636)", () => {
  it("computes the S256 challenge from the appendix B vector", async () => {
    expect(await codeChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
  });
  it("generates verifiers from the unreserved alphabet with high entropy", () => {
    const seen = new Set<string>();
    for (let i = 0; i < 50; i++) {
      const v = randomVerifier();
      expect(v).toMatch(/^[A-Za-z0-9\-._~]{64}$/);
      seen.add(v);
    }
    expect(seen.size).toBe(50);
    expect(() => randomVerifier(10)).toThrow();
  });
  it("base64url has no padding or url-unsafe characters", () => {
    expect(base64url(new Uint8Array([251, 255, 254]))).toBe("-__-");
  });
  it("builds a complete authorization request and keeps the secrets client-side", async () => {
    const { url, pending } = await buildAuthorizationRequest(CFG, "http://localhost:5173/auth/callback", "/runs");
    const u = new URL(url);
    expect(u.origin + u.pathname).toBe(CFG.authorization_endpoint);
    expect(u.searchParams.get("response_type")).toBe("code");
    expect(u.searchParams.get("client_id")).toBe("sdtf-ui");
    expect(u.searchParams.get("redirect_uri")).toBe("http://localhost:5173/auth/callback");
    expect(u.searchParams.get("scope")).toBe("openid profile");
    expect(u.searchParams.get("code_challenge_method")).toBe("S256");
    expect(u.searchParams.get("code_challenge")).toBe(await codeChallenge(pending.verifier));
    expect(u.searchParams.get("state")).toBe(pending.state);
    expect(u.searchParams.get("nonce")).toBe(pending.nonce);
    expect(url).not.toContain(pending.verifier);
    expect(pending.return_to).toBe("/runs");
  });
  it("refuses to start when the provider is not configured", async () => {
    await expect(buildAuthorizationRequest({ enabled: false }, "http://x/cb")).rejects.toThrow(/not configured/);
    await expect(buildAuthorizationRequest({ ...CFG, authorization_endpoint: "", error: "OIDC discovery failed: boom" }, "http://x/cb")).rejects.toThrow(/discovery failed/);
  });
});

describe("callback validation", () => {
  const pending = { verifier: "v".repeat(43), state: "st-1", nonce: "n-1", redirect_uri: "http://x/cb", return_to: "/", started_at: Date.now() };
  it("accepts a matching state and returns the code", () => {
    expect(checkCallback(parseCallback("?code=abc&state=st-1"), pending)).toEqual({ code: "abc", pending });
  });
  it("rejects state mismatch, missing pending login, provider errors and stale attempts", () => {
    expect(() => checkCallback(parseCallback("?code=abc&state=other"), pending)).toThrow(/state mismatch/);
    expect(() => checkCallback(parseCallback("?code=abc&state=st-1"), null)).toThrow(/no login in progress/);
    expect(() => checkCallback(parseCallback("?error=access_denied&error_description=User%20cancelled"), pending)).toThrow(/access_denied: User cancelled/);
    expect(() => checkCallback(parseCallback("?state=st-1"), pending)).toThrow(/code missing/);
    expect(() => checkCallback(parseCallback("?code=abc&state=st-1"), { ...pending, started_at: Date.now() - 11 * 60 * 1000 })).toThrow(/expired/);
  });
  it("decodes JWT payloads without trusting them and builds token/end-session requests", () => {
    const payload = btoa(JSON.stringify({ nonce: "n-1", name: "Jane" })).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    expect(unverifiedClaims(`eyJhbGciOiJSUzI1NiJ9.${payload}.sig`)).toEqual({ nonce: "n-1", name: "Jane" });
    expect(unverifiedClaims("garbage")).toEqual({});
    const body = tokenRequestBody(CFG, "abc", pending);
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code_verifier")).toBe(pending.verifier);
    expect(body.get("client_id")).toBe("sdtf-ui");
    const logout = new URL(endSessionUrl(CFG, "idt", "http://localhost:5173/")!);
    expect(logout.searchParams.get("id_token_hint")).toBe("idt");
    expect(logout.searchParams.get("post_logout_redirect_uri")).toBe("http://localhost:5173/");
    expect(endSessionUrl({ enabled: true }, null, "http://x/")).toBeNull();
  });
});

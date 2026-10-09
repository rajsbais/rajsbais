# ADR-0012 — Browser login: OIDC authorization code + PKCE, code exchange on the API

**Status:** accepted

## Context
The API already verified RS256 bearer tokens from an OpenID Connect provider (ADR-free increment, see
`docs/08-security-approval-model.md`), but the SPA could only sign in with seeded development users. Enterprise
deployments need the browser to obtain tokens from the corporate identity provider without SDTF ever seeing a
password, and without an implicit-flow token in the URL.

## Decision
1. **Authorization code flow with PKCE (RFC 7636, S256)** in the SPA. The verifier, `state` and `nonce` are
   generated with Web Crypto, kept in `sessionStorage` for the duration of the redirect only, and the challenge
   is sent to the provider. The flow therefore needs a secure context (https or localhost); the UI says so when
   `crypto.subtle` is missing.
2. **The API performs the code exchange by default** (`POST /auth/oidc/exchange`, `SDTF_OIDC_EXCHANGE=api`). The
   SPA forwards `code` + `code_verifier` + `redirect_uri` + `nonce`; the API calls the token endpoint, verifies the
   ID token (signature via JWKS, issuer, audience, expiry, nonce), chooses the bearer the SPA should use (the
   access token when it verifies against the API's issuer/audience, else the ID token), maps directory groups to
   SDTF roles, writes a `LOGIN` audit event and returns the session. Reasons: the provider's token endpoint does
   not have to be CORS-enabled for the SPA origin, confidential clients (client secret) work without exposing the
   secret, and the trust decision happens in one place. `SDTF_OIDC_EXCHANGE=browser` keeps a pure-SPA variant for
   providers that require it; the API then only verifies the tokens the browser obtained.
3. **Provider configuration is published, not baked in.** `GET /auth/oidc/config` returns issuer, client id,
   scopes, endpoints (explicit settings or OIDC discovery, cached) and whether dev login is enabled, so the same
   frontend build serves every environment.
4. **Silent refresh and RP-initiated logout.** When the provider issues a refresh token, the SPA refreshes one
   minute before expiry through `POST /auth/oidc/refresh`; sign-out clears the session and redirects to the
   provider's `end_session_endpoint` with `id_token_hint` when it has one.
5. **A test-only provider ships with the product** (`sdtf fake-idp`): discovery, consent page, PKCE-enforcing
   token endpoint, refresh, logout. It exists so the complete browser flow is exercised in CI-like conditions and
   demos without a Keycloak/Entra/Okta tenant. It performs no authentication and must never face users.

## Consequences
* Dev HMAC login remains available (`SDTF_DEV_USERS=1`) and is hidden behind a disclosure when SSO is on; it is
  disabled in the shipped compose/Kubernetes manifests.
* Tokens live in `localStorage` (same as before) so page reloads keep the session; XSS remains the threat to
  mitigate at the CSP level. Refresh tokens, when present, are stored alongside; providers can be configured to
  issue none for public clients.
* The API is now an OAuth client of the provider for the exchange step and needs network access to the token
  endpoint; the SPA only needs the authorization endpoint.
* Not covered: SAML, device/CIBA flows, front-channel logout notifications, token binding.

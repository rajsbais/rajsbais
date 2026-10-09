# 08 — Security and approval model

Implemented in `backend/sdtf/security/auth.py`, enforced on every route via `Depends(require("<perm>"))`.

## Identity
* Dev build: seeded users with PBKDF2 password hashes, HMAC-SHA256 signed bearer tokens with expiry
  (`SDTF_AUTH_SECRET`, `SDTF_TOKEN_TTL`). `SDTF_DEV_USERS=0` disables seeding in production images.
* OIDC SSO (`security/oidc.py`): RS256 bearer tokens are verified against the provider's JWKS (URL or file), issuer, audience and expiry; the groups claim is mapped to SDTF roles via `SDTF_OIDC_ROLE_MAP`, the tenant claim to the tenant. Unknown groups degrade to `viewer`. Dev HMAC tokens stay available for local use (`SDTF_DEV_USERS=1`). SAML is not supported.
* Browser login (ADR-0012): the SPA runs the **authorization code flow with PKCE (S256)** (`frontend/src/auth/`).
  `GET /auth/oidc/config` publishes issuer, client id, scopes and endpoints (explicit `SDTF_OIDC_*_ENDPOINT`
  settings or OIDC discovery). The SPA generates verifier/state/nonce with Web Crypto, redirects to the provider and
  returns to `/auth/callback`; by default the **API exchanges the code** (`POST /auth/oidc/exchange`, forwarding the
  verifier), verifies the ID token incl. nonce, picks the bearer (access token when it verifies against the API's
  issuer/audience, else the ID token), maps groups to roles and audits a `LOGIN`. `SDTF_OIDC_EXCHANGE=browser` lets
  the SPA call the token endpoint itself (needs CORS on the provider); the API then only verifies. Silent refresh via
  `POST /auth/oidc/refresh` when a refresh token was issued; sign-out performs RP-initiated logout at
  `end_session_endpoint`. A **test-only provider** (`python -m sdtf.cli fake-idp`) exercises the whole flow locally;
  it authenticates nobody and must never be exposed.

## RBAC + ABAC
| Role | Permissions |
|---|---|
| admin | * |
| architect | project:read/write, scope:write, rules:write, run:start, agent:run, records:read, data:unmasked |
| approver | project:read, approve:manifest, approve:rules, approve:run, agent:decide, records:read, data:unmasked |
| operator | project:read, run:start, run:resume, records:read |
| auditor | project:read, audit:read, records:read (masked) |
| viewer | project:read |
ABAC: tenant segregation on projects (`assert_project_access`), project-scoped resources (systems, manifests,
rulesets, runs) are checked against their project on every access.

## Four-eyes and production change control
* Manifest and ruleset creators cannot approve their own artefacts (403); approval requires zero pending dispositions
  and a valid content hash; approvals are stored (`approvals`) and audited.
* Runs require APPROVED manifest + ruleset; tampering (hash mismatch) is refused (409). Only SIMULATED mode exists.
* Reconciliation sign-off is split into TECHNICAL and BUSINESS records.

## Data protection
* Masking/tokenisation of name fields for principals without `data:unmasked` (`mask_payload`).
* Export-controlled materials are MANUAL_DISPOSITION until a compliance decision; the Compliance Agent flags
  cross-border transfers (company-code country vs target country), TSA retention and personal-data exposure.
* Evidence packages are hashed (`evidence_index.json`); the audit log is hash-chained and verifiable (`/audit/verify`).
* Secrets never live in the repository: compose/k8s read them from the environment / secret store.

## Agent safety
Agents receive stored evidence only; proposals are persisted with confidence and citations; `forbidden_actions`
(authorize_production_migration, delete_data, post_financial_adjustment, change_security_policy) are structural —
no agent has a code path to those operations.

## Security tests (backend/tests/test_api.py)
401 without/with tampered token · 403 for viewer writes and audit reads · creator cannot approve · PRODUCTION mode
refused · tampered manifest refused · masked records for auditor vs unmasked for architect.

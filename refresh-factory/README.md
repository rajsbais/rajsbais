# Keystone — SAP Intelligent Refresh Factory (MVP)

Selective, business-consistent SAP non-production refresh with masking, conflict control and reconciliation, for **SAP ECC 6.0** and **SAP S/4HANA**.

> **Honest status:** this is a working vertical slice running against a **simulated** SAP adapter with synthetic data (ECC EP1/100→EQ1/200 and S/4HANA S4P/100→S4Q/200).
> No real SAP system is read or written. See [`docs/01-capability-matrix.md`](docs/01-capability-matrix.md) for what is real, simulated, catalogued or planned.

## Run
```bash
cd backend && pip install -e '.[test]' && python -m pytest          # 595 tests
cd ../frontend && npm install && npm run build                       # UI served by the API from frontend/dist
cd ../backend && uvicorn rfactory.api.main:app --port 8088           # http://localhost:8088  (API docs at /docs). Not 8000: a local SAP system's HTTP port is 8000
# durable state (survives restarts, encrypted at rest):  RFACTORY_DATA_DIR=./data RFACTORY_STATE_KEY=$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())') uvicorn ...
# UI dev server: cd frontend && npm run dev   (proxies /api to :8000, edit frontend/vite.config.ts if you moved the API)
# UI end-to-end + accessibility tests (starts its own backend per test): cd frontend && npm run test:e2e
```
Sign in with the sidebar user switcher (demo header auth; `cora.regional` is a scoped steward limited to company 2000 and EP1/EQ1). Walkthrough: *Control tower → Load synthetic landscape → Selective designer* (create project, scope company 1000 / 90 days,
build plan, analyze conflicts, apply SKIP policy, apply masking advice, submit; switch to **Carol (Change approver)** to approve; switch back to execute) → *Reconciliation*.

## What the slice demonstrates
Register systems · discover company codes/plants/objects · scope by company code and date · expand master + document-flow dependencies · volume preview ·
conflict detection (identical/modified target objects, tester-owned documents, missing customizing, number ranges) · masking with discovery of custom PII fields ·
approval with separation of duties · checkpointed load, resume, rollback · 34+ technical/business/security checks with a release gate · report + evidence ZIP + hash-chained audit.

**Delta refresh:** *Delta refresh* screen → create the weekend QA sync, add the recommended masking rule (Dave), submit (Alice), approve (Carol), run; then *Simulate source activity* and preview/run again to see only the difference move.

**Remote source (RFC):** *Landscape → Connect demo remote source (fake RFC)* registers a second ECC production source reached through the RFC adapter over a **fake** transport; use it as the source in the Selective designer. The adapter is read-only (allow-listed function modules, bounded scans, WHERE pushdown). It has never run against a real SAP system; connecting a real one needs `pyrfc` and the SAP NetWeaver RFC SDK, which this repository does not install or test (`POST /api/systems/connect`).

**Manufacturing:** *Selective designer* → root business object `PRODUCTION_ORDER` (tick *MATERIAL_DOCUMENT* downstream) pulls the BOMs, component materials and goods issues/receipts; four production reconciliation checks run on the target. ECC uses MKPF/MSEG, S/4HANA uses MATDOC (one instance per document line). The test catalog adds make-to-stock templates (completed/open order, BOM) for subset and synthetic provisioning.

**Test data catalog:** *Test catalog* screen → create the default policy (Alice), submit, approve (Carol), request materials as Alice (subset), then as Tina request an O2C chain (subset or synthetic), record usage, release; curators can scan the target, make datasets golden and restore them.

**Lean client builder:** *Lean client* screen → create a template from a purpose preset (Alice), submit, approve (Carol), estimate lean vs full client copy, build client 320 into the QA system, then lock/unlock or decommission it.

**Post-copy factory:** *Post-copy* screen → capture a profile of the clean QA target (Alice), submit, approve (Carol), *simulate a system copy*, plan a run, approve as Basis lead (Bastian), Integration owner (Ingrid) and Security officer (Sven), execute (Alice), review the verification gate, roll back if needed.

**Authentication and scoping:** by default the API uses demo-header auth (no real authentication). Set `RFACTORY_AUTH=oidc` with `RFACTORY_OIDC_ISSUER`, `RFACTORY_OIDC_AUDIENCE` and `RFACTORY_OIDC_JWKS_FILE` (or `_URL`) to require signed bearer tokens (RS256/ES256) and ignore the demo header entirely. Roles come from the `roles` claim; `rf_kind` (`human`/`agent`/`service`), `rf_systems` and `rf_company_codes` carry the principal kind and the ABAC scope. Audit entries are HMAC-signed (`RFACTORY_AUDIT_KEY`); `GET /api/audit/head` returns a signed head to keep outside the platform. State is envelope-encrypted: rotate the data key online (`POST /api/persistence/rotate-data-key`) or the key-encryption key offline (`python -m rfactory.persistence.rotate`). For a browser login set `RFACTORY_OIDC_AUTHORIZE_URL`, `RFACTORY_OIDC_TOKEN_URL` and `RFACTORY_OIDC_CLIENT_ID` (optionally `RFACTORY_OIDC_SCOPE`, `RFACTORY_OIDC_END_SESSION_URL`): the UI then offers Authorization Code + PKCE (S256, public client, no secret; register the app's URL as the redirect URI; needs https or localhost for `crypto.subtle`); sessions end when the access token expires (no refresh tokens). Pasting a token still works. Tokens can be revoked (`POST /api/auth/logout`, `/api/auth/logout-all`, and `/api/auth/revoke` for a security officer; set `RFACTORY_OIDC_REQUIRE_JTI=1` to refuse tokens that cannot be revoked individually): the revocation list is local and does not end the session at the identity provider. Keys live in files or the environment (no KMS).

**UI tests:** `frontend/e2e` (Playwright + axe-core). Journeys drive the real UI through each module with role switching (separation of duties, disabled controls, unmasked-data warning, agent restrictions); accessibility specs scan all 17 views in empty and loaded states and check landmarks, one `h1` per view, heading order, skip link, keyboard navigation and sorting, WAI-ARIA tabs, visible focus, colour-token contrast, alert announcements, 320/390px reflow and dark/light schemes. Automated checks find only part of the accessibility problems; a manual screen-reader review has not been done. Set `PW_CHROMIUM` to use a specific Chromium binary.

Docs: [capability matrix](docs/01-capability-matrix.md) · [architecture](docs/02-architecture.md) · [backlog](docs/03-backlog.md) · [OpenAPI](docs/openapi.json) · [first contact with a real system](docs/04-real-system-test.md) · [ABAP loader (write side)](docs/05-abap-loader.md) · [deployment](docs/06-deployment.md) · [DB schema (design)](db/schema.sql).

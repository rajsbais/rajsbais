# Test report — current branch

## Automated suite (`cd backend && pytest`)
| Group | Tests | Result |
|---|---|---|
| Synthetic generator (determinism, balanced FI, carve-out patterns, key uniqueness) | 4 | pass |
| Rule engine (parse/validate, determinism+lineage, missing mapping, validation errors, dry run, rule factory) | 6 | pass |
| Graph & scope (document chains, traversal policies, manifest lifecycle incl. four-eyes/tamper, what-if, persistence) | 5 | pass |
| Pipeline (slice completes, evidence hashes, SpinCo-only target, financial checks present, audit chain, idempotent re-run, unapproved/tampered/production refused, failure + resume) | 9 | pass |
| API & security (health, 401/403 matrix, token tamper, full 8-step flow, masking) | 5 | pass |
| Scenario matrix (fiscal-year window, open-only, open-items-and-balances, cross-company exclude/reference, shared reference/exclude/manual, plant filter, two company codes, reverse carve-out) | 11 | pass, each with an explained reconciliation outcome |
| Failure injection (tampered target record → checksum/field/trial-balance FAIL; rejecting rule → line rejections at TRANSFORM, the journal entry API refuses the now-unbalanced documents at LOAD, trial balance stays consistent, variance explained by the refused amounts) | 2 | pass |
| Agents (12 registered, each bounded, decision workflow), scope recommendation validity, ownership proposals, carve-out reports, cutover runbook DAG | 17 | pass |
| Scale (scale 3 slice completes, timings recorded) | 1 | pass |
| UI end-to-end (Playwright, 18 screens, graph traversal, scope preview, cutover risk) | 1 | pass when run with `SDTF_E2E=1` against a live stack (verified in this session); skipped otherwise |
| OIDC (role mapping, expired/issuer/audience rejection, forged signature, API acceptance, tenant isolation) | 6 | pass |
| OIDC PKCE login, API side (public config from discovery without secrets, disabled state, code exchange with verifier forwarded and ID token + nonce verified, mapped roles, bearer accepted by the API, code replay refused, LOGIN audited; wrong verifier / stale nonce / missing fields rejected; refresh grant; browser-side exchange verification and forged ID token rejected) | 5 | pass (fake authorization server over an httpx mock transport enforcing S256) |
| OIDC PKCE login, browser (Playwright against `sdtf fake-idp`: SSO button from `/auth/oidc/config`, redirect with S256 challenge/state/nonce and no verifier in the URL, consent page, callback, API-side exchange with no browser call to the token endpoint, roles from directory groups, authenticated API calls, return to the originally requested page, RP-initiated logout, forged callback rejected) | 1 | pass when run with `SDTF_E2E=1 SDTF_E2E_OIDC=1` (verified in this session); skipped otherwise |
| Merger (cross-source duplicate detection, collision plan blocks naive rulesets, resolved plan runs both sources into one company code with group financial PASS; API merge flow) | 3 | pass |
| RFC adapter (simulated add-on as executable contract: snapshot required/expiry, S_TABU_NAM-style authorization and unknown tables, range-table predicate semantics, primary-key order + package clamp + checksums + resumable keyset cursor, cursor bound to table/predicate/snapshot; client checksum verification and cursor driving; RFC extractor produces the identical row set to the synthetic extractor with key/org pushdown and with org-only fallback; `build_extractor` by connector with actionable errors; full vertical slice over RFC reconciles PASS; pyrfc binding parameter/error mapping with a fake SDK and actionable error when absent; API registration refusing stored secrets, connector test, synthetic import rule, demo on the RFC path) | 12 | pass |
| Delta capture (CDC poll contract: paging, table/predicate filter with watermark advance on dropped events, invalid watermark, authorization, checksum, client cursor driving; apply order follows document flow; activity simulator persists balanced documents and the change log, honours the freeze, deterministic; engine: source drift visible before delta, capture splits scope with reasons, transform/apply/reconcile counts add up, applied events re-read from the target, baseline staging follows inserts/updates/deletes, second cycle captures nothing, ledger ignores replayed sequences, stale event conflicts and leaves the target untouched, freeze required for the final delta, frozen company codes block activity, final full reconciliation PASS and cutover-ready, no cycles after the final one, SYNTHETIC sources refused; API: state/backlog, simulate-changes, cycles with roles, freeze by approver, events, platform status) | 9 | pass |
| Delta loaders (simulated S/4 gateway: CSRF required, unknown service/entity, configuration validation of sales org and plant, duplicate 409; deep insert with derived total/status/billing company code, quantity re-pricing and header follow-up, stale ETag 412, read-only/not-updatable/key-immutable refusals, item and cascading header delete, master delete refused, persistence through the record store; journal entries: unbalanced and unknown company code refused, internal numbering, period derivation, derived customer open item, reversal with net zero, double reversal refused, external numbering; bindings round-trip losslessly and priced fields are never patched; loader maps change sets to deep insert / item PATCH with derived header / rejected non-updatable field / item and header delete / master block / journal post with target-assigned number / reversal / cockpit-only, config matched and missing, history refused; a delta cycle reports API calls and assigned numbers with staging following; API target registration, secret refusal, connector test, missing destination; HTTP transport: CSRF handshake, basic auth from env refs, OData create/get/patch with If-Match, 412, token rotation on 403, SOAP journal entry envelope and fault) | 8 | pass |
| Merger (resolved plan runs both sources through the journal entry API into one company code; the target numbers the entries and each carries its sending system in the source reference; group financial PASS) | — | covered in the Merger row |
| Initial load through the APIs (default load mode is api and the slice reconciles PASS with deep inserts, journal postings, business partner creates, cockpit paths for billing documents, histories of completed sales orders and goods-issued deliveries, target-assigned accounting document numbers written back to staging with the source reference kept on the entry; a second and third run of the same manifest post nothing twice and look journal entries up by reference; direct load mode still available through the API and `build_loader`; existing identical documents are duplicates and changed ones conflicts without writes; an HTTPS target refuses cockpit objects with the explanation; run request validates `load_mode`) | 6 | pass |
| Staging & workers (columnar contract, slice on columnar staging, two in-process workers over EXTRACT/TRANSFORM/LOAD jobs, lease expiry + re-queue, two `sdtf worker` subprocesses with concurrent load jobs and zero conflicts, pipelined vs barrier overlap, crash-between-jobs self-healing, reconciliation as per-table/functional/financial jobs with summary equal to the inline path) | 7 | pass |
| Object-store staging (fsspec `memory://` URL: contract + full slice; `file://` URL; clear error when the S3 driver is missing) | 2 | pass |
| Key-range index (Bloom filter has no false negatives, files outside the key range are never read, at most one file scanned for in-range keys, write-time dedup through the index, counts from sidecars, missing sidecar falls back to a scan) | 1 | pass |
| Observability (run/stage span tree and metrics for an inline run; job spans, staging-write child spans and pruning metrics for a distributed run; error status on a failed run; JSON logs carry trace ids; no-op when disabled; health reports telemetry status) | 6 | pass |
| Graph store (Cypher adapter round-trip through a fake Cypher executor: batched parameterised MERGEs, per-type labels, attribute round trip, traversal parity, search, isolation; neighbourhood query shape; relational parity; missing-URI error) | 4 | pass |
| Server-side traversal parity (five policy sets incl. REFERENCE/STOP/FLAG, depth limit, both directions, missing seed): identical inclusion ranks, stopped and missing sets to the in-process algorithm; bounded adjacency queries; scope evaluation streams nodes and delegates traversal | 6 | pass |
| Neo4j live integration | 1 | skipped unless `SDTF_NEO4J_URI` is set |
| **Total** | **138** | **135 passed, 3 skipped by default (UI e2e, OIDC e2e, Neo4j)** |

Frontend unit tests (`cd frontend && npm test`, vitest): PKCE S256 challenge against the RFC 7636 appendix B vector, verifier alphabet/entropy, base64url, authorization request contents (challenge, state, nonce; verifier never in the URL), refusal when unconfigured, callback validation (state mismatch, no pending login, provider error, missing code, expired attempt), unverified claim decoding, token/end-session request shapes: 8 pass.

Lint: `ruff check backend/sdtf backend/tests` clean. Frontend: `tsc --noEmit` and `vite build` clean.

## Scenario matrix outcomes (what the engine says, and why)
| Scenario | Reconciliation | Explanation |
|---|---|---|
| full carve-out of 5000 | PASS | — |
| fiscal years 2024–2025 only | WARN | GL variances fully attributed to documents outside the year window (balance carry-forward required) |
| open documents only | WARN | variances attributed to closed documents removed by the status filter |
| open items and balances | WARN | closed history retained by seller; carry-forward required |
| cross-company EXCLUDE / REFERENCE | WARN | intercompany accounts 141000/161000/410000/801000 retained; AR/AP open-item differences explained |
| shared masters REFERENCE | WARN | inventory valuation of reference-only materials stays in source (explained) |
| shared masters EXCLUDE | FAIL | *correct detection*: excluded masters are still referenced by transferred documents; the scope preview now warns before approval |
| shared masters MANUAL, plant filter, two company codes, reverse carve-out | PASS | — |

## Scale timing (synthetic, single process, SQLite)
| Scale | Rows generated | Graph | Objects in scope | Extracted records | Slice wall time |
|---|---|---|---|---|---|
| 1 | 11,086 | 2,976 nodes / 8,719 edges | 675 | 1,793 | ≈ 3 s |
| 2 | ≈ 21k | 5,571 / 17,138 | 1,047 | 3,147 | ≈ 2 s run after ≈ 1 s discovery + graph |
| 3 | ≈ 33k | 8,128 / 25,611 | 1,596 | 4,702 | 7.5 s |

Extraction stage throughput fell from ≈ 20k rec/s (scale 1) to ≈ 6.5k rec/s (scale 3) because staging writes go
through SQLite inside the API process; this is the expected bottleneck that the columnar staging backend and
distributed workers (backlog P0) remove. None of these numbers describe SAP extraction.

## Defects found and fixed during this campaign
1. Fiscal-year filters leaked through document flow because deliveries had no year attribute. Deliveries now
   carry `WADAT_IST`, and expanded documents outside the window are classified RETAINED_BY_SELLER.
2. Variance explanations ignored documents removed by year/status filters. A new "outside filters" bucket makes
   every scenario's GL variance fully explained.
3. Excluding shared masters silently broke referential integrity. Scope impact now carries
   `excluded_but_referenced` and a warning before approval.
4. The evidence directory was bound at import time. Settings are now late-bound so evidence always lands in the
   configured directory.
5. Some seeds produced no export-controlled material for the carve-out company code. The generator guarantees one
   per company code so the compliance path is always exercised.

## Not tested here
PostgreSQL (only SQLite is available in this environment; the models use portable types and the Alembic migration
applies, but a PostgreSQL run is outstanding) · Kubernetes manifests on a cluster · container builds (the CI workflow
does this) · any real SAP connectivity.

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
| Failure injection (tampered target record → checksum/field/trial-balance FAIL; rejecting rule → exceptions + explained variance) | 2 | pass |
| Agents (12 registered, each bounded, decision workflow), scope recommendation validity, ownership proposals, carve-out reports, cutover runbook DAG | 17 | pass |
| Scale (scale 3 slice completes, timings recorded) | 1 | pass |
| UI end-to-end (Playwright, 18 screens, graph traversal, scope preview, cutover risk) | 1 | pass when run with `SDTF_E2E=1` against a live stack (verified in this session); skipped otherwise |
| OIDC (role mapping, expired/issuer/audience rejection, forged signature, API acceptance, tenant isolation) | 6 | pass |
| Merger (cross-source duplicate detection, collision plan blocks naive rulesets, resolved plan runs both sources into one company code with group financial PASS; API merge flow) | 3 | pass |
| Staging & workers (columnar contract, slice on columnar staging, two in-process workers, lease expiry + re-queue, two `sdtf worker` subprocesses) | 5 | pass |
| **Total** | **75** | **74 passed, 1 skipped by default (e2e)** |

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

# 11 — First executable vertical slice: acceptance criteria and verified results

Run it: `cd backend && python -m sdtf.cli demo` (or `pytest`, or via the UI: Portfolio → Create demo project →
Landscape → Graph → Scope → Carve-out → Rules → Runs → Reconciliation → Compliance).

| # | Acceptance criterion | Evidence | Result |
|---|---|---|---|
| 1 | Import a synthetic ECC landscape | `tests/test_generator.py`, 11,086 rows / 50 tables at scale 1, balanced FI, cross-company patterns | ✅ |
| 2 | Discover company codes and plants | `test_full_api_flow`: 6 company codes, 9 plants, 2 controlling areas, interfaces, jobs, S/4 impacts | ✅ |
| 3 | Select a carve-out company code | scope definition `company_codes=["5000"]` | ✅ |
| 4 | Identify related business objects | graph 2,976 nodes / 8,719 edges; seeds + explainable expansion (`traces`) | ✅ |
| 5 | Detect shared and cross-company dependencies | SHARED_DUPLICATED masters, PARTIALLY_TRANSFERRED cross-company docs, intercompany balances, `test_scope_evaluation_and_manifest_lifecycle` | ✅ |
| 6 | Create a versioned scope manifest | v1/v2 auto-versioning, sha256 content hash, tamper detection, four-eyes approval, dispositions | ✅ |
| 7 | Apply transformation mappings | generated ruleset (company code, plant, controlling area, BP conversion, number range, ledger default, reject parked) validated + embedded tests | ✅ |
| 8 | Execute a simulated migration | 6-stage run, 37 partitions, 1,793 records staged, 1,788 loaded, 5 config matched, 0 exceptions; restart from checkpoint tested | ✅ |
| 9 | Reconcile source and target | 233 checks: TECHNICAL 190 PASS, FUNCTIONAL 13 PASS, FINANCIAL 30 PASS (trial balance, GL balances, AR/AP open items, assets, inventory, intercompany, currency, fiscal period) | ✅ |
| 10 | Generate an auditable execution report | report.json/md + manifest + ruleset + reconciliation evidence with SHA-256 index; hash-chained audit trail verified | ✅ |

Test suite: 29 tests (`backend/tests`), lint clean (`ruff`), frontend type-checks and builds, all 18 screens render
against the live API (Playwright smoke in the development session).

## What this slice is not
It is not a production migration. Extraction reads an in-platform record store, the loader writes to a simulated
target, and the numbers above come from synthetic data. See `docs/capability-status.md`.

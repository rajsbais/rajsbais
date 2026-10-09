# 10 — Prioritised development backlog

Legend: P0 next increment · P1 following · P2 later. Sizes are relative (S/M/L/XL).

## Phase 2–3 hardening (current foundation)
| P | Item | Size |
|---|---|---|
| ✅ | Distributed extraction workers: claim-based jobs with leases, `sdtf worker` pods + HPA (ADR-0010) | M |
| ✅ | Columnar staging backend: Parquet per run/table/partition on an object-storage mount (ADR-0009) | L |
| P0 | Transform/load as distributed jobs per partition (today finalisation runs in one worker) | M |
| P1 | S3/GCS staging via fsspec; per-file key-range manifests for random access | M |
| ✅ | OIDC SSO + group→role mapping (done: `security/oidc.py`); UI PKCE login flow pending | M |
| ✅ | Multi-source MERGER orchestration, key-collision planner, master-data dedup (done: `runtime/merge.py`, `catalog/dedup.py`) | L |
| P1 | Property-graph backend adapter (ADR-0004) with the same `Graph` interface | M |
| P1 | Scope designer: saved scenarios, scenario matrix comparison across >2 manifests | S |
| P1 | Carve-out: asset vs share deal templates, residual cleanup execution with approval workflow | M |
| P1 | Rule editor: visual mapping grid, lookup table upload, per-rule approval | M |

## Phase 4 — SAP connectivity
| P | Item | Size |
|---|---|---|
| P0 | ABAP add-on: `Z_SDTF_READ_PACKAGE`, `Z_SDTF_OPEN_SNAPSHOT`, metadata module; RFC adapter | XL |
| P0 | Discovery against real DDIC (DD02L/DD03L, table sizes via DBSTATC/HANA views), real org tables | M |
| P1 | CDS/ODP adapter for S/4 sources; OData adapter for masters | L |
| P1 | Loaders: Business Partner API, Product API, Journal Entry SOAP, Migration Cockpit staging | XL |

## Phase 5 — Business object packages & reconciliation
| P | Item | Size |
|---|---|---|
| P0 | Finance: Universal Journal posting path, open-item migration objects, asset balances via migration object | L |
| P1 | BOM, routing, work center, batches, serial numbers, scheduling agreements, contracts | L |
| P1 | QM, PM, PS, EWM, TM packages | XL |
| P1 | Reconciliation: period-end trial balance from ACDOCA, FX revaluation, material ledger valuation | M |

## Phase 6 — NDT & cutover
| P | Item | Size |
|---|---|---|
| P0 | CDC adapter (change pointers/CDHDR) + delta replay engine with ordering and idempotency | XL |
| P0 | Benchmark harness and benchmark report (docs/benchmarks.md) | M |
| P1 | Cutover execution tracking, incident escalation, resource assignment, mock cutover management | M |

## Phase 7 — AI & factory
| P | Item | Size |
|---|---|---|
| P1 | LLM reasoner behind `Reasoner` interface (evidence bundle only), prompt/eval harness | M |
| P1 | Rule factory v2: learn mappings from approved rulesets across projects | M |
| P2 | Migration factory portfolio KPIs, cross-project benchmarks | S |

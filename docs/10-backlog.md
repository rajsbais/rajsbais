# 10 — Prioritised development backlog

Legend: P0 next increment · P1 following · P2 later. Sizes are relative (S/M/L/XL).

## Phase 2–3 hardening (current foundation)
| P | Item | Size |
|---|---|---|
| ✅ | Distributed extraction workers: claim-based jobs with leases, `sdtf worker` pods + HPA (ADR-0010) | M |
| ✅ | Columnar staging backend: Parquet per run/table/partition on an object-storage mount (ADR-0009) | L |
| ✅ | Transform/load as distributed jobs per partition with stage barriers (ADR-0010) | M |
| ✅ | Per-partition pipelining across stages (default for DISTRIBUTED runs; barrier mode optional) | M |
| ✅ | Reconciliation as distributed jobs: technical per table, functional, financial | M |
| ✅ | S3/GCS/Azure staging via fsspec (`sdtf[s3]`, `sdtf[gcs]`) | M |
| ✅ | Per-file key-range sidecar index with Bloom filter for membership and counts on object stores | M |
| ✅ | OIDC SSO + group→role mapping (done: `security/oidc.py`) | M |
| ✅ | UI-side OIDC login: authorization code + PKCE, API-mediated exchange, refresh, RP-initiated logout, test-only provider (ADR-0012) | M |
| ✅ | Multi-source MERGER orchestration, key-collision planner, master-data dedup (done: `runtime/merge.py`, `catalog/dedup.py`) | L |
| ✅ | OpenTelemetry traces/metrics/JSON logs (ADR-0011) | M |
| ✅ | Property-graph backend adapter: Neo4j store behind `GraphStore` (ADR-0004) | M |
| ✅ | Server-side policy traversal in Neo4j (frontier traversal with identical policy semantics); scope evaluation no longer loads edges | M |
| P1 | Scope designer: saved scenarios, scenario matrix comparison across >2 manifests | S |
| P1 | Carve-out: asset vs share deal templates, residual cleanup execution with approval workflow | M |
| P1 | Rule editor: visual mapping grid, lookup table upload, per-rule approval | M |

## Phase 4 — SAP connectivity
| P | Item | Size |
|---|---|---|
| ✅ | RFC adapter against the add-on contract: transports (pyrfc, simulated add-on), pushdown, keyset packages, snapshot token, checksums, connector test endpoint, `demo --connector RFC` (ADR-0013) | L |
| P0 | ABAP add-on activation: compile/review `sap-abap/src/` on an SAP development system, authorizations, transport; live verification of the adapter | XL (needs an SAP system) |
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
| ✅ | CDC adapter against the add-on contract (`Z_SDTF_CDC_POLL`, simulated + ABAP reference) and delta replay engine with scope filter, atomic change sets, dependency ordering, idempotency ledger, conflicts, freeze, final delta + full reconciliation, backlog monitor (ADR-0014) | XL |
| ✅ | Delta loaders through released APIs: API connector, bindings, loader, simulated S/4 gateway, HTTPS/SOAP transport, connector test (ADR-0015) | L |
| P0 | Activate `Z_SDTF_CDC_POLL` on a real release; validate the API bindings against a real target's `$metadata`, OAuth client, journal entry SOAP namespaces | XL (needs SAP systems) |
| ✅ | Initial load through the same API loaders (deep inserts, journal postings with target numbering and idempotent source reference, cockpit path for histories and cockpit objects; `load_mode=direct` kept) | M |
| ✅ | Migration cockpit staging-file export (CSV per staging table, SpreadsheetML workbook per migration object, manifest with checksums, zip; API, UI and CLI) for real targets, since staging tables cannot be posted over HTTPS | M |
| ✅ | Cockpit export from the target's own migration object templates: register the release's XML template per project, automatic column mapping with coverage report and overrides, filled template in the package (verified against illustrative samples) | M |
| ✅ | Validate the template parser and filler against the real template layout: parser aligned with SAP's documented layout (hidden rows 4–6, key span in row 7, descriptions in row 8, hidden SAP Structure/SAP Field columns), filled files processed by SAP's own XML file splitter in the test suite, `cockpit-template check` for downloads (docs/cockpit-template-validation.md) | S |
| ✅ | Alias catalogue extended: 500+ BAPI-style template names from the public interface structures (`catalog/fields.py`) with DDIC descriptions for every catalog field; project aliases learned from a registered template's Field List (DDIC description match, disagreeing global aliases flagged), confirmed by an architect through API, CLI and the Runs page, consulted before the global catalogue | S |
| ✅ | Migration object lookup per release: catalogue of documented objects with renames and availability per S/4HANA release (unverified `SIF_*` ID hints), per-table resolution for accounting documents, project registry imported from the target's object list (API, CLI, Runs page), name/ID/source/confidence in the export manifest | S |
| P3 | Run `sdtf cockpit-template check` on templates downloaded from a target release, confirm the aliases their Field Lists propose, and import the release's object list so the migration object IDs are authoritative (needs system access; none available here) | S |
| P0 | Benchmark harness and benchmark report (docs/benchmarks.md) | M |
| P1 | Cutover execution tracking, incident escalation, resource assignment, mock cutover management | M |

## Phase 7 — AI & factory
| P | Item | Size |
|---|---|---|
| P1 | LLM reasoner behind `Reasoner` interface (evidence bundle only), prompt/eval harness | M |
| P1 | Rule factory v2: learn mappings from approved rulesets across projects | M |
| P2 | Migration factory portfolio KPIs, cross-project benchmarks | S |

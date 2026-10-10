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
| ✅ | Rule editor: grid of the rules in words with inline editing, reordering and removal, lookup tables from CSV exports (duplicates and conflicts reported), structured document composed into the canonical YAML and back, per-rule approval / rejection under four eyes with decisions carried into the next version for unchanged rules; set approval refused while a rule is rejected (migration 0012) | M |
| ✅ | Scope designer: scenario matrix across up to eight manifests of a project (classification counts side by side, identical / differing objects with the objects that differ, pairwise deltas; `GET /projects/{id}/manifests/matrix`, Scope designer card with the manifests ticked) | S |
| ✅ | Carve-out deal templates (asset deal, share deal, hive-down: policies, residual rule, obligations, approvals; deviations assessed, never refused) and residual cleanup plans (items from the candidates, decisions with notes, four-eyes business approval refused until the manifest is approved and a run reconciled, work package export, execution on the simulated source with the package written first; refused on an add-on source; migration 0014) | M |

## Phase 4 — SAP connectivity
| P | Item | Size |
|---|---|---|
| ✅ | RFC adapter against the add-on contract: transports (pyrfc, simulated add-on), pushdown, keyset packages, snapshot token, checksums, connector test endpoint, `demo --connector RFC` (ADR-0013) | L |
| P0 | ABAP add-on activation: compile/review `sap-abap/src/` on an SAP development system, authorizations, transport; live verification of the adapter | XL (needs an SAP system) |
| ✅ | Discovery through the add-on (ADR-0017): organisational and interface tables read in full, catalogue tables sized with Z_SDTF_TABLE_METADATA (DB_GET_TABLE_SIZE), custom tables from DD02L / DD02T / DD03L, distributions counted with Z_SDTF_AGGREGATE, the complete instance index read with the dependent rows by key, a labelled quick-look sample the scope engine refuses, unreadable tables listed; route by connector with override, CLI | M |
| ✅ | Preflight before the first live test (`sdtf preflight`, `POST /systems/{id}/preflight`, Connect page): SDK, destination and secret in the environment, DNS, port, RFC logon, Z_SDTF_* modules, add-on handshake, table authorisations; API destination, TLS, catalogue and bound services; a fix per failure, reads only | S |
| ✅ | Discovery through the add-on without the line-item tables: open-item status from BSID / BSIK, material-document company codes from MSEG aggregates (`read.lean`); the inventory equals the record-store discovery | M |
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
| ✅ | Upload simulation feedback import: the app's message log (CSV/TSV/JSON/SpreadsheetML) matched to exported instances by key, key columns or key search; rejected instances marked `COCKPIT_ERROR` with COCKPIT-stage exceptions; classified summary and pass rate per object; retry package of rejected instances; API, CLI, Runs page; illustrative sample log | M |
| ✅ | Retry package re-upload tracking: every export is a round with manual upload/migration marks, feedback attached per round with per-instance outcomes, released instances back to LOADED, superseded rounds, burn-down and convergence across rounds; API, CLI, Runs page | M |
| P3 | Run `sdtf cockpit-template check` on templates downloaded from a target release, confirm the aliases their Field Lists propose, import the release's object list, and import a real simulation log so the column recognition is confirmed (needs system access; none available here) | S |
| ✅ | Reconciliation through the adapters (ADR-0016): source over the RFC add-on with `Z_SDTF_AGGREGATE` read-integrity evidence, target read back over the released APIs, not-verified reporting, re-run on a completed run | M |
| ✅ | Aggregate-only reconciliation for large scopes: GL, open-item, asset, inventory and intercompany totals computed in the source by `Z_SDTF_AGGREGATE`, only the retained documents' lines transferred, explanation buckets from the staging, automatic above `SDTF_RECON_AGGREGATE_ABOVE`, `mode` on the re-reconcile endpoint / CLI / page | M |
| ✅ | Read services for asset values (fixed-asset read service) and material valuation (product valuation entity) on the target: the asset and inventory checks compare real values instead of WARN | S |
| ✅ | Metadata verification tool: EDMX parser (V2/V4), expectations per service, deviations with suggestions, gateway catalogue probe, `POST /metadata/check`, `POST /systems/{id}/connector/metadata-check`, `sdtf metadata`; public references checked for the journal item, product valuation and fixed-asset bindings (docs/metadata-verification.md); `PriceControl` corrected to `PriceDeterminationControl`; inventory guard when the valuation entity has no stock value | S |
| ✅ | RFC read-back on the target: an API target registered with `meta.rfc` too serves T001, T001K, asset values and loaded history tables through the read-only add-on (company-code / loaded-key pushdown, count evidence), reported in the connector test | S |
| ✅ | Aggregate-only reconciliation on the target: with the add-on on the target the journal line items are not read back, totals are computed there (`Z_SDTF_AGGREGATE`), target trial balance as evidence, auto by the target's journal size, `mode` applies to both sides | S |
| ✅ | ACDOCA-based aggregates for S/4HANA systems (leading ledger, signed amounts normalised, open items and intercompany from the journal lines, asset measure declared, detection by product and rows, meta override) | S |
| ✅ | Asset acquisition values on S/4HANA: compatibility view FAAV_ANLC, else APC line items of ACDOCA / FAAT_DOC_IT by movement category (configured per system), else net postings declared as such | S |
| ✅ | Business process analysis from the database footprint: TAANA-style variants with the 80 % line, DB05 selectivity, age profile with retention horizon, DB02 growth, DB15 links, company-code pushdown, unreadable tables reported; Enterprise analyzer tab, API, CLI, Markdown report | M |
| ✅ | Import of an ST03N transaction-profile export (delimited, header detected, SAP number formats) compared with the footprint: transactions mapped to business objects, write / read steps, steps per document from the discovery statistics, unmapped transactions listed; stored on the system, usage block of the analysis and its report, API, CLI, analyzer card | S |
| ✅ | Transformation journey page: five phases with deliverable status derived from the platform state, validated outputs, S/4HANA approach view with the project's approach marked | S |
| ✅ | Transform Factory front end: eleven-section shell (sidebar / top bar), executive dashboard with quick carve-out, landscape explorer, carve-out studio generator with the immutable manifest, company-code dependency view, system connections (destinations without secrets, handshakes), ABAP extraction view, rules with source / preview, nine-stage CDC view, cutover gates, audit report | M |
| ✅ | Read configuration front end: per-system journal table / ledger / asset chain / inventory chain managed on the Landscape page through a validated, audited endpoint (transport and destination untouched); measures used shown on the Reconciliation page | S |
| ✅ | RFC read-back of the inventory values on S/4HANA: MBEW through the Material Ledger proxy view, else the Material Ledger period totals CKMLCR / CKMLHD by valuation area (period and currency type configured per system), else the inventory accounts of the Universal Journal declared as a different measure; rows and aggregate mode | S |
| P1 | Run the metadata check against A4H and correct the bindings it flags; confirm on A4H which FAA_MOVCAT values carry APC, whether FAAV_ANLC is readable by the technical user, and that the add-on's SELECT on MBEW returns the Material Ledger stock values (proxy view MBV_MBEW) (needs the VM; cannot be done here) | S |
| ✅ | Benchmark harness (`sdtf bench --scales 1,2,3`): generation, both discovery paths, graph, scope evaluation, run stages with extraction throughput, reconciliation, end to end; the measured section of docs/benchmarks.md names the environment and is replaced on every run, with the JSON beside it | M |
| ✅ | Mock cutover management: cutover rehearsal checklist (automatic readiness items, manual items, waivers, task timings fed into the forecast, lessons, approver verdict, report); API, CLI, Cutover page | M |
| ✅ | Live cutover execution tracking: timeline against the clock with task timings observed from the platform's own runs (initial load, delta cycles, final delta, reconciliation), hand timings winning, late minutes, projection, downtime clock; incidents with an escalation path per task owner (HIGH / CRITICAL refuse GO, CRITICAL escalated at once); task assignments with backup and contact; API, CLI, Cutover page (migration 0013). No SAP system, scheduler or ticketing tool is read and nobody is paged | M |

## Phase 7 — AI & factory
| P | Item | Size |
|---|---|---|
| ✅ | LLM reasoner behind the `Reasoner` interface (ADR-0018): redacted evidence bundle only, Anthropic Messages API or any OpenAI-compatible endpoint over httpx, key from a named environment variable, guard against numbers absent from the facts, fallback to the heuristic text with the outcome recorded on every proposal; `GET /agents/reasoner`, evaluation harness `sdtf llm-eval`. HTTP contract verified with a mocked transport only, never against a live provider in this build | M |
| ✅ | Rule factory v2: mappings learned from the approved rule sets of the tenant's other projects (per field, agreement required, conflicts listed and never proposed, organisational fields excluded by default), proposed as learned lookups and rules with provenance in the description; API, CLI, Rules workbench switch | M |
| ✅ | Migration factory portfolio KPIs (`GET /platform/portfolio/kpis`): per project the manifests, rule sets, runs, last reconciliation, extraction throughput, rehearsals and GO verdicts, open incidents, cleanup plans and a derived phase; totals by phase; cross-project benchmarks of the completed runs (extraction rec/s, run seconds), labelled as simulated on this build; Portfolio page tiles | S |

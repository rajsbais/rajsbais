# Capability review — SAP carve-outs: what is built, what is pending, what cannot be built here

Reviewed against the master build prompt (sections A–S) on the current branch. Three honest buckets:

* **Built** — implemented and verified by automated tests on synthetic data in this repository.
* **Pending** — engineering work that *can* be done in this codebase and is on the backlog (docs/10).
* **Cannot be built by Claude in this environment** — requires things that do not exist here: a real SAP
  system, SAP licences and transports, customer data, measured benchmarks, legal/compliance sign-off, or
  commercial certification. These need a customer landscape, SAP Basis/ABAP access and human accountability.

## A. Transformation scenarios
| Capability | Built | Pending | Cannot be built here |
|---|---|---|---|
| Company-code / plant / time / status / object filters, open vs closed handling | ✅ scope DSL, 11-scenario matrix test | — | — |
| Org restructuring, CoA harmonisation, BP conversion, number ranges | ✅ rule DSL + factory | visual mapping grid | validated CoA map for a real customer chart |
| Cross-module dependency preservation | ✅ graph traversal with explanations | S/4-source resolvers (CDS) | — |
| Bluefield prepared-shell target, config matching, release registry, load-method selection | ✅ | finance conversion execution, CVI execution, custom-code analysis | SAP Readiness Check / Simplification Item catalogue integration (SAP-licensed content) |
| Forward/reverse, full/partial carve-out; ParentCo/SpinCo ownership; shared masters; cross-company docs; IC balances; TSA; residual cleanup candidates; export control | ✅ (reverse carve-out verified: 15.6k records, 130 financial checks PASS) | asset vs share deal templates; automated residual cleanup *execution* | legal disposition decisions, data-ownership sign-off |
| Mergers: multi-source, dedup, number-range conflicts | ✅ merge groups, cross-system key-collision planner (header + item keys), master-data dedup with survivor redirection, per-source number ranges/prefixes, group-level financial reconciliation (two-source test: PASS) | fuzzy matching beyond exact normalised keys; UI-driven survivor selection | — |

## B–E. Discovery, graph, scope, carve-out intelligence
| Capability | Status |
|---|---|
| Release/DB/OS, clients, org hierarchy, tables/keys/fields/sizes, custom Z tables, volumes by year/org, interfaces, jobs, S/4 impacts | **Built** against the synthetic record store; **cannot be run against a real SAP system here** (needs RFC/ABAP add-on, doc 02) |
| Archiving/retention status, growth statistics over time | **Pending** (needs real DBSTATC / archive info; synthetic has none) |
| Dependency graph, traversal policies, explainability, missing/shared/duplicate/cross-boundary detection; property-graph (Neo4j) store adapter | **Built**; Neo4j adapter not exercised against a live server here |
| Scope designer, real-time impact preview, what-if compare, versioned immutable manifest, no execution without approval | **Built** |
| Carve-out classification (7 classes), detections, completeness and residual reports, explicit business dispositions | **Built** |

## F–L. Rules, extraction, NDT, object library, S/4 intelligence, reconciliation, cutover
| Capability | Status |
|---|---|
| Rule DSL: deterministic, testable, versioned, validated, lineage, dry run, approval | **Built** |
| Extraction: full/selective, partitioning, parallel workers, checkpoint/restart, throttling, snapshot token | **Built** on the synthetic store and through the **RFC adapter** (predicate pushdown, keyset packages, consistency token, checksums; pyrfc or simulated add-on, ADR-0013); ABAP reference sources written but **not compiled**; **CDS/OData/File adapters pending**; **verification against a real SAP system cannot be done here** (needs the add-on transported into a customer system, the NW RFC SDK and a technical user) |
| Streaming, compression, encryption in transit | **Pending** (object-storage staging backend) |
| Target loads through released APIs (ADR-0007) | **Built for the initial load and delta cycles** (ADR-0015: API bindings, loader, simulated S/4 gateway as executable contract, migration cockpit path for histories/cockpit objects, HTTPS transport with CSRF/OAuth/SOAP); **cannot be validated against a real S/4HANA tenant here** (no system, no `$metadata`, no OAuth client); cockpit staging-file export **built** (CSV/SpreadsheetML/manifest/zip, same routing as the LOAD stage) and **template-driven export built** (registered app templates parsed, auto-mapped with a coverage report, filled); **a template downloaded from a release has not been seen here** (SAP distributes them only through the app or SAP Note 2470789); the parser and filler are verified against SAP's documented layout and against SAP's own XML file splitter executed on filled files, and `cockpit-template check` reports any deviation of a download (docs/cockpit-template-validation.md) |
| Near-zero-downtime: delta capture, continuous sync, backlog, freeze, final delta | **Built (simulated)**: CDC contract on the add-on, capture with scope reasons, atomic change sets, dependency-ordered idempotent apply, conflicts, freeze, final delta with full reconciliation, backlog/lag (ADR-0014); **real change-document coverage per release and downtime claims cannot be established here**: they require the add-on on a customer system and measured benchmarks |
| Object packages FI/SD/MM/PP incl. tests | **Built (simulated)**; QM/PM/PS/EWM/TM/MDG/industry **pending** |
| S/4 conversion intelligence: CVI, Universal Journal, MATDOC, new Asset Accounting, ML, MATNR length, custom fields | **Built** (registry + mapping); credit management and CO-PA **pending**; **released-API loaders cannot be exercised here** (no S/4 system, no API client certificates) |
| Reconciliation technical/functional/financial, variance explanations, evidence packages, sign-offs | **Built** (233 checks on demo; tamper and rule-rejection detection tested); **reads through the adapters** (source over the RFC add-on with aggregate read-integrity evidence, target back over the released APIs; tables without a read path reported as not verified) **verified on the simulated add-on / gateway and a mocked HTTPS endpoint only** |
| Cutover runbook, critical path, downtime forecast, go/no-go, rollback gates, point of no return, cutover rehearsal checklist (automatic + manual items, task timings into the forecast, lessons, approver verdict) | **Built (planning and rehearsal tracking)**; live execution tracking, incidents, resources **pending**; mock cutover on a real landscape **cannot be done here** (the checklist tracks it once people run it) |

## M–Q. Agents, security, architecture, frontend, NFRs
| Capability | Status |
|---|---|
| 12 bounded agents with confidence, evidence, approvals, decision log | **Built** (heuristic); LLM reasoner **pending** |
| RBAC/ABAC, tenant segregation, four-eyes, masking, immutable audit, tamper detection, OIDC SSO (JWKS, group→role map), browser login with authorization code + PKCE (ADR-0012) | **Built** (PKCE flow verified in a real browser against the bundled test-only provider); secrets manager integration, tokenisation service **pending**; **validation against a customer's Keycloak/Entra/Okta tenant cannot be done here** |
| Export-control and cross-border controls | **Built** as classification + compliance findings; **legal determinations cannot be made by the platform** |
| 18 frontend applications on live APIs | **Built** (Playwright e2e verified) |
| Containers, Kubernetes, CI/CD, IaC | **Built** manifests/workflow; **not exercised on a real cluster here** (no cluster); Terraform/Helm **pending** |
| Multi-terabyte scale, HA, horizontal workers, observability | **Built**: distributed workers (ADR-0010) and columnar staging (ADR-0009). **Pending**: nothing in this row; S3/GCS and OTLP export verified only against in-memory filesystems/exporters, not live services. Measured only to scale 3 synthetic (≈33k rows); real-volume benchmarks need a real source |

## What Claude cannot deliver from this environment, explicitly
1. **Any connection to a real SAP system** (read or write): no SAP instance, RFC SDK, SAP Cloud Connector, or credentials exist here, and none should be supplied to a development sandbox.
2. **The ABAP add-on itself installed in a customer system**: the contract exists (`sap-abap/README.md`); writing, transporting and authorising ABAP objects needs an SAP development system and a Basis team.
3. **Performance or downtime guarantees**: only synthetic in-memory timings exist; real numbers need the benchmark protocol in `docs/benchmarks.md` on customer hardware.
4. **Legal, tax and compliance decisions**: export-control clearance, data-transfer impact assessments, TSA terms, asset vs share deal structures. The platform records and enforces decisions; humans make them.
5. **SAP certification / partner validation** of load methods and the add-on.
6. **Production cutover accountability**: go/no-go, business sign-off and rollback invocation remain human decisions by design (agents are structurally forbidden from authorising them).

## Verdict
The semantic core the brief asks to build first (dependency graph + selective scope engine + carve-out studio)
is built and tested, with a working simulated transformation runtime and full reconciliation behind it. The
product is a credible Phase 1–3 foundation. It is **not** a production SAP migration tool until the SAP-side
adapters, loaders and benchmarks are delivered against a real landscape, which this environment cannot provide.

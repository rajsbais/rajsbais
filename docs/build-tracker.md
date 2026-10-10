# Build tracker

Living tracker of the SDTF build: what was completed, what is in progress, what is pending and how each pending
item will be built. Status words: DONE (committed, CI green), IN PROGRESS (code on the branch, not yet pushed),
NEXT (offline, can be built without an SAP system), BLOCKED-VM (needs the NPL / A4H VMs), LATER (after the first
live test). Everything marked DONE is verified on the simulators only unless the row says otherwise; the honest
status of every capability is in `capability-status.md`, the item-level backlog in `10-backlog.md`.

Last updated: 2026-10-10 (branch `claude/clever-maxwell-ues8be`, PR #1).

## 1. Completed today (2026-10-10)

| # | Increment | Commit | What it gives the user |
|---|---|---|---|
| 1 | Transform Factory front end (eleven-section shell, every page wired to the live API) | 44e7de4 | the interface shown in the screenshots |
| 2 | Transformation journey page with live deliverable status and the S/4HANA approach view | 7a7fad5 | phase view on the dashboard |
| 3 | Business process analysis from the database footprint through the add-on | a14a9a1 | Process analyzer page |
| 4 | Discovery through the add-on, benchmark harness, scenario matrix | 0362078 | discovery without reading BSEG / MSEG; `sdtf bench`; manifest comparison |
| 5 | Rule editor: grid, lookup tables from CSV, per-rule decisions | 6e4f4c5 | Rules workbench |
| 6 | Live cutover execution: timeline, observed timings, incidents, assignments | c701e60 | Cutover page, live execution block |
| 7 | LLM reasoner behind the Reasoner interface, with an evaluation harness | 05ffdda | optional LLM narrative; heuristic by default |
| 8 | Carve-out deal templates and residual cleanup plans | 0c862ea | Carve-out studio, residual exposure tab |
| 9 | ST03N workload import and portfolio KPIs | dc3b32d | usage in the analyzer, Portfolio page |
| 10 | Lean add-on discovery and rule factory v2 (learned mappings) | 3d7eeee | FI status from BSID / BSIK; rules learned from other projects |
| 11 | Preflight before the first live test | 5b04050 | Connect page "Preflight" button, `sdtf preflight` |
| 12 | Period-end reconciliation (trial balance per period, cut-off, foreign-currency items, valuation per area, price control) | 88c7c45 | Finance page, financial layer |
| 13 | Manufacturing masters: BOM, routing, work center, batch | this push | Landscape and Carve-out pages list the four object types; cockpit export routes them to their migration objects |

Test suite at the last push: 237 tests, 234 passed, 3 skipped by default (UI e2e, OIDC e2e, Neo4j). CI green on
every pushed head today.

## 2. In progress now

| Item | Status | Where it stands |
|---|---|---|
| Manufacturing masters: bills of material, routings, work centers, batches as plant-scoped master data | DONE (this push) | tables, object types with SAP's own identities (STKO, PLKO, CRHD, MCH1), relationships, generator, migration objects, merger number ranges and batch prefix, plant-view filtering in the extraction; 4 tests; full suite green; benchmark regenerated (345 checks at scale 3). |

## 3. Pending until the product build is complete

### 3a. Offline (no SAP system needed) — built next, in this order

| # | Item | Size | Build plan |
|---|---|---|---|
| O1 | Scheduling agreements and contracts | M | EKKO / EKPO rows with BSTYP L / K become `MM.SchedulingAgreement` and `MM.Contract`; the object key stays EBELN but the header filter on BSTYP keeps the three purchasing objects apart (today EKKO is the purchase order header only). Generator adds a few agreements with delivery schedules (EKET) and contracts with release orders; graph links release order → contract; open-document validity check extended; migration objects "Purchase scheduling agreement" / "Purchase contract" resolved. Tests: generator, discovery counts, scope, run, reconciliation. |
| O2 | Serial numbers | S | EQUI / SERI with the equipment master as header, batch-like plant scoping through the plant of the equipment; relationship material → serial number; cockpit object "Equipment". |
| O3 | CDS / ODP extraction adapter for S/4HANA sources (simulated) and OData adapter for masters | L | a second source transport next to the RFC add-on: CDS views read through the ABAP CDS reader interface of the add-on or ODP extraction (`RODPS_REPL_*` contract), with the same partitioning, pushdown and count evidence as the RFC path; a simulator of the contract for the tests; OData reads of business partner / product masters for the discovery sample. Metadata check extended to the CDS views. |
| O4 | Loader contracts: Business Partner API, Product API, Journal Entry SOAP, Migration Cockpit staging tables | XL | the simulated targets already accept the payloads; this item builds the real client code (OData batch with CSRF token, SOAP envelope for journal entries, staging-table inserts through the add-on) behind the existing load-method selection, with recorded fixtures from the public API documentation and the same idempotency keys. Verification against A4H is a BLOCKED-VM item (V4). |
| O5 | QM, PM, PS, EWM, TM packages | XL | one package at a time in the order PM (equipment, functional location, maintenance order), QM (inspection lot, results), PS (project, WBS, network), EWM (warehouse stock, HU), TM (freight order). Each package = tables + object types + relationships + generator data + load method + reconciliation checks + tests, as the manufacturing package was built. |
| O6 | Front end for the new objects | S per package | the catalogue pages read the object registry, so new objects appear automatically; package-specific checks need their reconciliation rows named on the Finance page. |

### 3b. Blocked on the VMs (NPL / A4H on the AIPC host; the offline backup is still running)

| # | Item | Size | Plan once the VMs are up |
|---|---|---|---|
| V1 | First live test on NPL | S | `sdtf preflight` against the registered NPL system (SDK, destination `env:` password, port 3300, logon), then handshake, discovery through the add-on, metadata check. Fix whatever the preflight names. |
| V2 | ABAP add-on activation | XL | import `sap-abap/src/` into NPL (SE80 or abapGit), review the SELECTs against the real DDIC, authorisations for the technical user, transport; then run the discovery and an extraction of one company code and compare the counts with the add-on's count evidence. |
| V3 | A4H bindings | S | run the metadata check against A4H, correct the bindings it flags; confirm which FAA_MOVCAT values carry APC, whether FAAV_ANLC is readable by the technical user, and that the add-on's SELECT on MBEW returns the Material Ledger stock values (MBV_MBEW). |
| V4 | Real loads and CDC | XL | activate `Z_SDTF_CDC_POLL` on NPL; load one carved-out company code into A4H through the released APIs and the migration cockpit staging (O4); run the delta cycle; reconcile with the RFC read-back. |
| V5 | Cockpit templates and simulation log | S | download the templates of A4H's release, run `sdtf cockpit-template check`, confirm the aliases, import the object list and a real simulation log so the column recognition is confirmed. |
| V6 | Cutover rehearsal on the VMs | M | one timed rehearsal of the runbook against NPL → A4H with the observed timings feeding the downtime forecast. |

### 3c. Later (after the first live test)

| # | Item | Plan |
|---|---|---|
| L1 | Deployment on a cluster | the Docker / compose / Kubernetes manifests exist but were never exercised on a cluster; run them on one node with the simulators, then with the VMs. |
| L2 | Neo4j graph adapter at scale | the adapter is tested behind a skip; run the graph tests against a Neo4j container in CI. |
| L3 | Fuzzy duplicate matching and UI survivor selection for mergers | today exact normalised keys only. |
| L4 | Production use | out of scope for this build: the platform never writes into a production SAP system until V1–V6 are passed and a customer sign-off process exists. |

## 4. Definition of done for the product build

1. Every row of `capability-status.md` reads IMPLEMENTED or SIMULATED with a named simulator; nothing is asserted
   without a test.
2. V1–V5 passed on NPL / A4H: discovery, extraction, one load and one delta cycle verified against real systems,
   with the preflight, metadata check and count evidence recorded in the evidence package.
3. O1–O5 built with the same package pattern and their tests in the suite.
4. The front end shows each of these from the platform's own state (no hand-asserted status).

## 5. How to follow the tracker

- This file is updated with every increment; the commit list above is the change log.
- `docs/test-report.md` carries the test count per area; `docs/benchmarks.md` the measured section the benchmark
  harness rewrites.
- The PR check-in re-arms itself after each push and reports CI status.

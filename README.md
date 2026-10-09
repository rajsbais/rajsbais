# SDTF — SAP Selective Data Transformation Factory

An original, metadata-driven platform for SAP carve-outs, divestitures, mergers, selective data transitions and
Bluefield transformations. This repository contains **executable source code** (backend, frontend, tests,
migrations, deployment) for the Phase 1 foundation and the first vertical slice.

> **Honesty note.** Everything here runs against a synthetic ECC-like landscape and a simulated S/4HANA target.
> No SAP system is read or written. The RFC adapter is verified against a simulated add-on, not a live system. Per-area status (IMPLEMENTED / SIMULATED / PARTIAL / PLANNED / UNSUPPORTED) is
> maintained in [`docs/capability-status.md`](docs/capability-status.md) and shown in the product (`/platform/capabilities`).

## What works today (verified)
* Synthetic ECC 6.0 EHP8-like landscape (6 company codes, 9 plants, shared masters, cross-company sales/POs/stock
  transfers, intercompany postings, balanced FI, custom Z tables, interfaces, jobs).
* Discovery: system facts, organisational hierarchy, table statistics, business object inventory, S/4 impacts,
  complexity score, scope estimates.
* Business object dependency graph with explainable policy-driven traversal, stored relationally or in a Neo4j
  property graph (Sales Order → Delivery → Goods Issue →
  Billing → Accounting Document → Clearing; PO → GR → IR → FI; Production Order → Consumption → Confirmation).
* Selective scope designer → classification (fully / partially transferred, shared duplicated, retained, reference,
  excluded, manual disposition) → impact preview → **versioned, hashed, four-eyes-approved manifest**; what-if compare.
* Carve-out studio: ParentCo/SpinCo buckets, detections, intercompany balances, completeness and residual reports,
  business dispositions.
* Declarative YAML transformation rule DSL with validation, embedded tests, dry run, lineage; rule factory.
* Simulated migration run: partitioned/parallel/checkpointed extraction → transformation → idempotent load →
  **technical, functional and financial reconciliation** (233 checks PASS on the demo) → hash-chained audit and
  evidence package. Failed runs resume from checkpoints.
* 12 bounded AI agents (heuristic reasoner) that propose, with confidence and evidence; humans decide.
* Multi-source merger: merge groups, cross-system key-collision planning, master-data deduplication with survivor
  redirection, per-source number ranges, group-level financial reconciliation.
* Distributed stage workers: extraction, transformation and load as pipelined per-partition jobs and
  reconciliation as per-table, functional and financial jobs, with leases, crash re-queue, atomic stage closing
  and idle self-healing; columnar Parquet staging on local, S3, GCS or Azure filesystems via fsspec. INLINE threads and relational
  staging remain for small scopes.
* OpenTelemetry traces, metrics and trace-correlated JSON logs across API and workers.
* RBAC/ABAC, tenant segregation, masking, four-eyes approvals, tamper detection, OIDC single sign-on with a
  browser PKCE login (authorization code flow, API-mediated exchange, refresh, provider logout).
* 19 frontend applications wired to the API; Docker/compose/Kubernetes/CI.

## Quick start
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e "backend[dev]"
cd backend && pytest -q                      # 135 tests (+ opt-in UI e2e, OIDC e2e and Neo4j integration)
python -m sdtf.cli demo                      # full vertical slice, prints the execution report
python -m sdtf.cli demo --connector RFC      # same slice extracted through the RFC adapter on the simulated SAP add-on
python -m sdtf.cli serve                     # API http://localhost:8000/docs
cd ../frontend && npm install && npm run dev # UI http://localhost:5173 (login architect/architect)
```
Single sign-on locally (test-only provider, see ADR-0012):
```bash
python -m sdtf.cli fake-idp --port 9400 &
SDTF_OIDC_ISSUER=http://127.0.0.1:9400 SDTF_OIDC_JWKS_URL=http://127.0.0.1:9400/certs SDTF_OIDC_AUDIENCE=sdtf-ui \
  SDTF_OIDC_ROLE_MAP='{"SAP-Migration-Architects":"architect"}' python -m sdtf.cli serve
```
Or: `docker compose -f deploy/docker-compose.yml up --build` (UI :8080, API :8000, PostgreSQL).

## Documentation
| Doc | Content |
|---|---|
| [00](docs/00-capability-decomposition.md) | capability decomposition and bounded contexts |
| [01](docs/01-component-architecture.md) | component architecture |
| [02](docs/02-sap-connectivity-design.md) | source/target SAP connectivity design |
| [03](docs/03-canonical-business-object-model.md) | canonical business object model and packages |
| [04](docs/04-dependency-graph-schema.md) | dependency graph schema and traversal |
| [05](docs/05-transformation-rule-dsl.md) | transformation rule DSL |
| [06](docs/06-carve-out-scenario-model.md) | carve-out scenario model |
| [07](docs/07-ndt-cdc-consistency-recovery.md) | NDT/CDC consistency and recovery: delta cycles, freeze, final delta (simulated) |
| [08](docs/08-security-approval-model.md) | security and approval model |
| [09](docs/09-repository-deployment-architecture.md) | repository and deployment architecture |
| [10](docs/10-backlog.md) | prioritised backlog |
| [11](docs/11-vertical-slice-acceptance.md) | vertical slice acceptance criteria and verified results |
| [capability review](docs/capability-review.md) | built / pending / cannot be built here, per brief section |
| [test report](docs/test-report.md) | test campaign results, scenario matrix, scale timings, defects fixed |
| [connect real systems](docs/connect-real-systems.md) | step-by-step first contact with a real ECC (RFC, reads only) and S/4HANA (released APIs), what each step touches, what to send back |
| [UI walkthrough](docs/ui-walkthrough.md) | screenshots of the cockpit export, templates, aliases and migration object lookup on the running app, with the commands to run it |
| [cockpit template validation](docs/cockpit-template-validation.md) | what the template-driven cockpit export is verified against (SAP's documented layout, SAP's XML file splitter) and what still needs a downloaded template |
| [ADRs](docs/adr) | eleven architecture decision records |
| [operations](docs/operations.md) · [benchmarks](docs/benchmarks.md) · [capability status](docs/capability-status.md) | |

## Repository layout
`backend/` FastAPI + SQLAlchemy domain packages, Alembic, pytest · `frontend/` Vite/React/TS · `deploy/` containers,
compose, k8s · `docs/` · `sap-abap/` add-on interface contract (planned) · `.github/workflows/ci.yml`.

## Licence and originality
Independently engineered; inspired only by publicly documented functional categories of the SAP transformation
market. No third-party proprietary code, algorithms or UI are reproduced.

# 00 — Product capability decomposition and bounded contexts

SDTF (SAP Selective Data Transformation Factory) is decomposed into bounded contexts that map 1:1 to Python
packages under `backend/sdtf/`. Each context owns its data, exposes a service API, and is reachable through the
HTTP API (`backend/sdtf/api/`). Status legend: **IMPLEMENTED** (works, tested) · **SIMULATED** (works end to end on
synthetic data but replaces an SAP system with an in-platform store) · **PARTIAL** · **PLANNED** · **UNSUPPORTED**.

| # | Bounded context | Package | Owns | Status |
|---|---|---|---|---|
| 1 | Project & landscape registry | `models`, `api/routes_core` | projects, tenants, systems, connectors | IMPLEMENTED |
| 2 | Synthetic SAP landscape | `synthetic` | deterministic ECC-like generator, prepared S/4 shell | IMPLEMENTED |
| 3 | SAP metadata catalog | `catalog/tables`, `catalog/business_objects` | DDIC subset, canonical business objects, relationships, S/4 compatibility registry, load methods | IMPLEMENTED |
| 4 | Enterprise discovery | `discovery` | system facts, org hierarchy, table statistics, object inventory, interfaces, S/4 impacts, complexity, estimates | IMPLEMENTED (against record store) |
| 5 | Dependency graph | `graph` | instance graph, explainable policy traversal, neighbourhoods | IMPLEMENTED (relational persistence) |
| 6 | Selective scope & manifest | `scope` | scope DSL (Pydantic), evaluation, classification, impact, versioned immutable manifest, dispositions, four-eyes approval, what-if comparison | IMPLEMENTED |
| 7 | Carve-out intelligence | `carveout` | ParentCo/SpinCo buckets, detections, intercompany balances, completeness & residual reports | IMPLEMENTED |
| 8 | Transformation rules | `rules` | YAML DSL, compiler/validator, deterministic engine with lineage, embedded tests, dry run, rule factory | IMPLEMENTED |
| 9 | Extraction | `runtime/extraction`, `runtime/adapters`, `runtime/rfc` | partitioned, parallel, checkpointed extraction; RFC adapter with pushdown, snapshot token, checksums; adapter contracts | IMPLEMENTED (RFC client, verified on the simulated add-on) / SIMULATED (synthetic store) / OData-CDS PLANNED |
| 10 | Load | `runtime/load` | idempotent upsert, duplicate/conflict detection, config matching, load-method selection by target | SIMULATED |
| 11 | Reconciliation | `reconciliation` | technical, functional, financial checks with variance explanations | IMPLEMENTED |
| 12 | Audit & evidence | `audit` | hash-chained events, evidence packages, approvals | IMPLEMENTED |
| 13 | Run orchestration | `runtime/pipeline` | stage machine, preconditions, checkpoints, restart | IMPLEMENTED (SIMULATED mode only) |
| 14 | Cutover | `cutover` | runbook generation, critical path, downtime forecast, rollback gates | PARTIAL |
| 15 | Delta / NDT | — | CDC adapters, delta replay, backlog monitoring | PLANNED (design in doc 07) |
| 16 | AI agents | `agents` | 12 bounded agents, proposals, confidence, evidence, human decision | IMPLEMENTED (heuristic reasoner); LLM reasoner PLANNED |
| 17 | Security & governance | `security` | dev auth, OIDC SSO, RBAC/ABAC, tenant segregation, masking | IMPLEMENTED |
| 19 | Merger orchestration | `runtime/merge`, `catalog/dedup` | merge groups, collision planner, dedup, group reconciliation | IMPLEMENTED (simulated runtime) |
| 18 | Frontend applications | `frontend/` | 19 screens wired to the API | IMPLEMENTED |

## Context map (who depends on whom)
```
synthetic ─► catalog.store ─► discovery ─► graph ─► scope ─► carveout
                                                   │           │
                        rules ◄── rules.factory ◄──┘           │
                          │                                     ▼
                          └──► runtime(extract→transform→load) ─► reconciliation ─► audit/evidence
                                             ▲                            │
                                 security (every API call)              agents (read-only over all of the above)
```
Contexts communicate through persisted records (PostgreSQL/SQLite via SQLAlchemy), never through shared mutable
in-memory state, so each can be scaled or replaced independently (e.g. a property-graph backend for context 5).

## Scenario coverage (section A of the brief)
| Scenario | What exists today | What is missing |
|---|---|---|
| A1 Selective Data Transition | org/time/status/object filters, dependency preservation, CoA & BP mapping, validation | target process redesign tooling, broader object packages |
| A2 Bluefield / hybrid | prepared-shell target with config matching, release registry, load-method selection | finance conversion execution, CVI execution, custom-code analysis |
| A3 Carve-outs | forward/reverse flag, full/partial, shared masters, cross-company docs, IC balances, TSA, residual cleanup candidates, export control | asset vs share deal templates, automated cleanup execution (intentionally manual) |
| A4 Mergers | merge groups with key-collision planning, per-source number ranges and prefixes, master-data dedup, group financial reconciliation | fuzzy matching, UI survivor selection |

# 01 — Component architecture

## Runtime view
```
┌──────────────── Browser (React/TS, 18 apps) ────────────────┐
│  fetch /api/v1/*  bearer token                               │
└──────────────┬───────────────────────────────────────────────┘
               ▼
┌──────────────── API (FastAPI, backend/sdtf/api) ─────────────┐
│ auth · projects · discovery · graph · scope · carveout       │
│ rules · runs · audit · agents · cutover · platform           │
│  Depends(require("<permission>")) on every route             │
└──────┬───────────────┬───────────────┬───────────────────────┘
       ▼               ▼               ▼
 Domain services   Run orchestrator   Agents (read-only)
 discovery/graph/  pipeline: PRECHECK→EXTRACT→TRANSFORM→LOAD→RECONCILE→REPORT
 scope/carveout/   ThreadPool workers · checkpoints · restart
 rules/recon
       ▼
┌──────────────── Persistence (SQLAlchemy 2, PostgreSQL / SQLite) ─────────────┐
│ sap_records (source+target stores) · discovery · graph_nodes/edges            │
│ scope_manifests · rule_sets · migration_runs/run_stages · staged_records     │
│ reconciliation_results · audit_events (hash chain) · approvals · agent_decisions │
└───────────────────────────────────────────────────────────────────────────────┘
       ▼
 Evidence directory (object storage in production): report.json/md, manifest.json, ruleset.yaml, reconciliation.json, evidence_index.json
```

## Key design rules
1. **Everything executable goes through an approved, hashed manifest and an approved, validated ruleset.** The
   pipeline refuses DRAFT, REJECTED or tampered artefacts (`runtime/pipeline.py::start_run`).
2. **Semantic over structural.** Scope is computed on the business object graph, not on table filters. Every
   expanded object carries a trace (`policy`, `edge`, `reason`).
3. **Classification before extraction.** Extraction only sees the manifest's classification; partially transferred
   cross-company documents are split at the company-code boundary; reference-only masters become stubs.
4. **Three independent reconciliation layers.** A technical PASS never implies functional or financial PASS.
5. **Append-only governance.** Audit events are hash-chained; manifests are state machines (DRAFT→APPROVED/REJECTED),
   never edited after approval.
6. **Adapters are explicit about status.** `ADAPTER_REGISTRY` and `/platform/capabilities` expose IMPLEMENTED /
   SIMULATED / PLANNED / UNSUPPORTED so no screen can present a mock as production capability.

## Sequence: vertical slice
```
architect  POST /projects/demo            → synthetic ECC landscape + S/4 shell imported
architect  POST /systems/{src}/discover   → snapshot: org units, table stats, object inventory, S/4 impacts
architect  POST /systems/{src}/graph/build→ 2.9k nodes / 8.7k edges (scale 1)
architect  POST /projects/{p}/scopes/evaluate  (preview)  → impact & classification
architect  POST /projects/{p}/manifests   → v1 DRAFT, sha256
approver   POST /manifests/{m}/dispositions {all_pending, TRANSFER}
approver   POST /manifests/{m}/approve    → APPROVED (four-eyes enforced)
architect  POST /projects/{p}/rulesets/generate → candidate YAML; POST /rulesets → validated DRAFT
approver   POST /rulesets/{r}/approve
operator   POST /projects/{p}/runs        → SIMULATED run, 6 stages, reconciliation PASS, evidence package
auditor    GET  /runs/{run}/evidence, GET /audit/verify
```

## Scaling path (not yet exercised)
* Extraction workers become a distributed pool (Celery/Arq on Redis or a Kubernetes Job per partition); the
  partition and checkpoint model already exists.
* `RecordStore` gains a columnar/object-storage backend (Parquet partitions keyed by table/company code/year);
  the interface (`rows`, `lookup`, `get`) stays.
* Graph persistence can move to a property graph (ADR-0004) once instance counts exceed what relational
  adjacency queries handle comfortably (tens of millions of edges).

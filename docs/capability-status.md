# Capability status (keep current on every milestone)

| Capability | Status | Notes |
|---|---|---|
| Synthetic ECC landscape generator | IMPLEMENTED | deterministic, scalable, carve-out patterns, balanced FI |
| Landscape discovery & Enterprise Analyzer | IMPLEMENTED | runs on record store; real DDIC/stat sources planned |
| Org structure explorer | IMPLEMENTED | |
| Business object catalog & S/4 compatibility registry | IMPLEMENTED | 26 object types, 9 compatibility items |
| Dependency graph + explainable traversal | IMPLEMENTED | relational persistence; property graph planned |
| Scope designer, impact preview, what-if compare | IMPLEMENTED | |
| Versioned immutable manifest, dispositions, four-eyes approval | IMPLEMENTED | |
| Carve-out classification, completeness, residual exposure, IC balances | IMPLEMENTED | cleanup never executed automatically |
| Transformation rule DSL, validation, tests, dry run, rule factory | IMPLEMENTED | |
| Extraction | SIMULATED | synthetic store adapter; RFC/OData/CDS/File planned |
| Distributed stage workers: extraction, transformation and load as per-partition jobs with per-partition pipelining (claim/lease, crash re-queue, atomic stage closing, idle self-healing) | IMPLEMENTED | tested with in-process and separate worker processes; reconciliation runs in the advancing worker |
| Columnar staging (Parquet, object-storage mount) | IMPLEMENTED | parity-tested against relational staging |
| Transformation stage with lineage & exceptions | IMPLEMENTED | |
| Load | SIMULATED | idempotent upsert, config matching, load-method selection; released-API loaders planned |
| Reconciliation (technical/functional/financial) | IMPLEMENTED | on simulated data |
| Audit trail (hash chain), approvals, evidence packages | IMPLEMENTED | |
| Run restart / checkpoint recovery | IMPLEMENTED | partition-level for extraction, stage-level otherwise |
| Delta capture / continuous sync / NDT | PLANNED | design in docs/07 |
| Cutover command center | PARTIAL | runbook, critical path, forecast, rollback gates, go/no-go; execution tracking planned |
| AI agents (12) | IMPLEMENTED | heuristic reasoner; LLM reasoner planned |
| Security: RBAC/ABAC, tenant segregation, masking, OIDC SSO (RS256/JWKS, group→role map) | IMPLEMENTED | dev users remain for local use |
| Multi-source merger: merge groups, key-collision planning, master-data dedup, group financial reconciliation | IMPLEMENTED | simulated runtime |
| Frontend (19 applications incl. Merger & Consolidation) | IMPLEMENTED | all wired to live API |
| Deployment: Docker, compose, Kubernetes, CI | IMPLEMENTED | not yet exercised on a cluster |
| Production SAP migration | UNSUPPORTED | this build never connects to or writes into an SAP system |

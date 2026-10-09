# Capability status (keep current on every milestone)

| Capability | Status | Notes |
|---|---|---|
| Synthetic ECC landscape generator | IMPLEMENTED | deterministic, scalable, carve-out patterns, balanced FI |
| Landscape discovery & Enterprise Analyzer | IMPLEMENTED | runs on record store; real DDIC/stat sources planned |
| Org structure explorer | IMPLEMENTED | |
| Business object catalog & S/4 compatibility registry | IMPLEMENTED | 26 object types, 9 compatibility items |
| Dependency graph + explainable traversal | IMPLEMENTED | relational store by default; Neo4j property-graph adapter with server-side frontier traversal, verified at Cypher level (policy parity) and via opt-in integration test |
| Scope designer, impact preview, what-if compare | IMPLEMENTED | |
| Versioned immutable manifest, dispositions, four-eyes approval | IMPLEMENTED | |
| Carve-out classification, completeness, residual exposure, IC balances | IMPLEMENTED | cleanup never executed automatically |
| Transformation rule DSL, validation, tests, dry run, rule factory | IMPLEMENTED | |
| Extraction | IMPLEMENTED (RFC client) / SIMULATED (source) | synthetic store adapter; RFC adapter against the ABAP add-on contract (pyrfc or simulated add-on) verified end-to-end on the simulated add-on only; ABAP reference sources not compiled; OData/CDS/File planned |
| Distributed stage workers: extraction, transformation and load as per-partition pipelined jobs, reconciliation as per-table/functional/financial jobs (claim/lease, crash re-queue, atomic stage closing, idle self-healing) | IMPLEMENTED | tested with in-process and separate worker processes; only report rendering runs in the closing worker |
| Columnar staging (Parquet on local, S3, GCS, Azure or memory filesystems via fsspec) | IMPLEMENTED | parity-tested against relational staging; object-store path tested with the in-memory filesystem, not a live bucket |
| Transformation stage with lineage & exceptions | IMPLEMENTED | |
| Load | SIMULATED | idempotent upsert, config matching, load-method selection; released-API loaders planned |
| Reconciliation (technical/functional/financial) | IMPLEMENTED | on simulated data |
| Audit trail (hash chain), approvals, evidence packages | IMPLEMENTED | |
| Run restart / checkpoint recovery | IMPLEMENTED | partition-level for extraction, stage-level otherwise |
| Delta capture / continuous sync / NDT | SIMULATED | CDC through the add-on contract (`Z_SDTF_CDC_POLL`) over RFC, ordered idempotent replay with conflict detection, business freeze, final delta + full reconciliation, backlog/lag monitor; verified on the simulated add-on only; ABAP reference not compiled; no downtime figure claimed (ADR-0014) |
| Cutover command center | PARTIAL | runbook, critical path, forecast, rollback gates, go/no-go; execution tracking planned |
| AI agents (12) | IMPLEMENTED | heuristic reasoner; LLM reasoner planned |
| Security: RBAC/ABAC, tenant segregation, masking, OIDC SSO (RS256/JWKS, group→role map), browser PKCE login with API-side code exchange, refresh and provider logout | IMPLEMENTED | verified end-to-end against the bundled test-only provider, not yet against a live Keycloak/Entra/Okta tenant; dev users remain for local use |
| Multi-source merger: merge groups, key-collision planning, master-data dedup, group financial reconciliation | IMPLEMENTED | simulated runtime |
| Frontend (19 applications incl. Merger & Consolidation) | IMPLEMENTED | all wired to live API |
| Observability: OpenTelemetry traces, metrics, trace-correlated JSON logs | IMPLEMENTED | OTLP/HTTP export; verified with in-memory exporters, not against a live collector |
| Deployment: Docker, compose, Kubernetes, CI | IMPLEMENTED | not yet exercised on a cluster |
| Production SAP migration | UNSUPPORTED | this build never connects to or writes into an SAP system |

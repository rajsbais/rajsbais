# ADR-0003: PostgreSQL (SQLite in dev) via SQLAlchemy 2 + Alembic for configuration and transactional metadata
**Status:** Accepted

## Decision
All governance data (projects, systems, manifests, rulesets, runs, stages, staging index, reconciliation results,
approvals, audit chain, agent decisions) lives in PostgreSQL; SQLite is used for development and CI to keep the slice
dependency-free. JSON columns hold flexible payloads (manifest selection, rule compilation, metrics).

## Consequences
+ ACID state machine for manifests/runs; one backup unit; Alembic migrations.
− The generic `sap_records` table is a convenience for the synthetic store; at scale, extracted data belongs in
  partitioned object storage (backlog P0), not in the metadata database.

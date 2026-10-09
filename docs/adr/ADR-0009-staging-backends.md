# ADR-0009: Pluggable staging backends; columnar Parquet staging on an object-storage mount
**Status:** Accepted

## Context
Staging extracted rows in the metadata database (`staged_records`) is simple and transactional but does not scale to
multi-terabyte scopes, bloats the governance database, and is opaque to external tooling.

## Decision
A `StagingBackend` contract (`backend/sdtf/staging/base.py`) with two implementations: `RelationalStaging`
(default for development) and `ColumnarStaging`, which writes one zstd-compressed Parquet file per (run, table,
partition) under `SDTF_STAGING_DIR`, an object-storage mount (RWX PVC today; S3/GCS through a CSI driver or fsspec,
planned). Payloads are JSON columns beside key/status columns so Spark, DuckDB or pandas can read staging directly.
The backend is chosen per run and recorded in `run.metrics.staging_backend` so every worker and stage agrees.

## Consequences
+ Staging volume leaves PostgreSQL; files are immutable per partition except for stage rewrites; parallel writers
  need no row locks; (table, key) uniqueness is enforced on write (first copy wins).
− Transform/load rewrite whole partition files rather than updating rows; acceptable because stages are
  partition-oriented. Row-level random access goes through `keys()` + file scans; indexes are planned (per-file
  key ranges in a manifest).

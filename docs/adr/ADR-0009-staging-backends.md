# ADR-0009: Pluggable staging backends; columnar Parquet staging on an object-storage mount
**Status:** Accepted

## Context
Staging extracted rows in the metadata database (`staged_records`) is simple and transactional but does not scale to
multi-terabyte scopes, bloats the governance database, and is opaque to external tooling.

## Decision
A `StagingBackend` contract (`backend/sdtf/staging/base.py`) with two implementations: `RelationalStaging`
(default for development) and `ColumnarStaging`, which writes one zstd-compressed Parquet file per (run, table,
partition) on any fsspec filesystem named by `SDTF_STAGING_DIR`: a local path or RWX volume, `file://`,
`s3://bucket/prefix` (s3fs), `gs://bucket/prefix` (gcsfs), Azure (adlfs) or `memory://` for tests. Filesystem
options (S3-compatible `endpoint_url`, anonymous access) come from `SDTF_STAGING_FS_OPTIONS`; credentials come from
the environment, instance roles or workload identity, never from the platform database. Object-store writes are
single atomic PUTs; local writes use temp-and-rename. Payloads are JSON columns beside key/status columns so Spark, DuckDB or pandas can read staging directly.
The backend is chosen per run and recorded in `run.metrics.staging_backend` so every worker and stage agrees.

## Consequences
+ Staging volume leaves PostgreSQL; files are immutable per partition except for stage rewrites; parallel writers
  need no row locks; (table, key) uniqueness is enforced on write (first copy wins).
− Transform/load rewrite whole partition files rather than updating rows; acceptable because stages are
  partition-oriented. Row-level random access goes through `keys()` + file scans, which on object stores means one
  GET per file; a per-file key-range index in a run manifest is the planned mitigation. Real S3/GCS behaviour
  (latency, eventual listing consistency on some providers) is not exercised here: tests use the in-memory and
  local filesystems.

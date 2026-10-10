# ADR-0011: OpenTelemetry for traces, metrics and trace-correlated logs
**Status:** Accepted

## Context
Distributed runs span API pods and many worker pods; stage timings persisted on the run are not enough to debug a
slow partition, a stuck lease or an object-store latency problem.

## Decision
A thin wrapper (`backend/sdtf/observability.py`) over the OpenTelemetry SDK exposes `span()`, `timed()`, `counter()`
and `histogram()` that degrade to no-ops when telemetry is disabled or the SDK is absent, so domain code is
instrumented unconditionally. Export is OTLP/HTTP, enabled by `OTEL_EXPORTER_OTLP_ENDPOINT`; `SDTF_OTEL_CONSOLE=1`
prints telemetry in development. FastAPI and SQLAlchemy are auto-instrumented. Logs can be JSON
(`SDTF_LOG_FORMAT=json`) and always carry `trace_id`/`span_id`.

Instrumented: `sdtf.run` → `sdtf.stage.<STAGE>` (inline runs); `sdtf.job` per worker job with stage, partition,
worker and attempt; `sdtf.staging.write_partition`; reconciliation check outcomes; stage closes; lease re-queues.
Metrics: `sdtf.runs`, `sdtf.stage.duration`, `sdtf.stage.<records|loaded|rejected|conflicts|checks>`, `sdtf.jobs`,
`sdtf.job.duration`, `sdtf.job.records`, `sdtf.jobs.requeued`, `sdtf.stage.closed`, `sdtf.staging.records_written`,
`sdtf.staging.files_pruned|files_scanned`, `sdtf.reconciliation.checks`.

## Consequences
+ One collector endpoint gives traces across API and workers, SLO-ready metrics and correlated logs; tests assert
  the span tree and metrics with in-memory exporters.
− Provider objects can be set once per process; re-configuration reuses handles. Sampling and cardinality limits
  (partition ids as span attributes, never as metric labels) are the operator's responsibility; metric labels are
  kept to stage/status/table.

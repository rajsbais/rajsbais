# ADR-0010: Claim-based distributed extraction workers with leases
**Status:** Accepted

## Context
Extraction must scale horizontally and survive worker loss. A message broker (Kafka/Redis) is an operational
dependency not every SAP landscape team wants on day one.

## Decision
Jobs live in the metadata database (`extraction_jobs`: one per partition *and stage* EXTRACT / TRANSFORM / LOAD).
`sdtf worker` processes claim a job with a conditional UPDATE (exactly one winner), hold a lease, process the
partition against the shared staging backend and mark the job DONE. Expired leases are re-queued; attempts are
counted. When every job of the current stage is DONE, the worker that notices flips the run to ADVANCING with
another conditional UPDATE, aggregates the job metrics into the stage, enqueues the next stage's jobs (or runs
reconciliation and the report after LOAD), so each stage transition happens exactly once. Stage barriers keep
de-duplication and reconciliation semantics identical to INLINE runs. Load jobs are concurrency-safe: they check
only their partition's keys and insert with conflict-ignore semantics, re-reading rows that lost an insert race.
INLINE execution (threads in the API process) remains for small scopes.

## Consequences
+ No broker; works on SQLite in development and PostgreSQL in production; crash recovery is tested.
+ Workers are stateless pods behind an HPA; the queue is observable (`/runs/{id}/jobs`, `/platform/workers`).
− Polling adds latency (`SDTF_WORKER_POLL_SECONDS`); a broker-backed queue can replace `claim_job` later behind the
  same functions. Reconciliation and the report still run inside the advancing worker; per-partition pipelining
  (transforming a partition as soon as it is extracted) is a possible later optimisation.

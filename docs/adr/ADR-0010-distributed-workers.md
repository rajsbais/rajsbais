# ADR-0010: Claim-based distributed extraction workers with leases
**Status:** Accepted

## Context
Extraction must scale horizontally and survive worker loss. A message broker (Kafka/Redis) is an operational
dependency not every SAP landscape team wants on day one.

## Decision
Jobs live in the metadata database (`extraction_jobs`, one per partition). `sdtf worker` processes claim a job with a
conditional UPDATE (exactly one winner), hold a lease, extract into the shared staging backend and mark the job
DONE. Expired leases are re-queued; attempts are counted. The worker completing the last job flips the run to
FINALIZING with another conditional UPDATE and executes transformation, load, reconciliation and report, so
finalisation happens exactly once. INLINE execution (threads in the API process) remains for small scopes.

## Consequences
+ No broker; works on SQLite in development and PostgreSQL in production; crash recovery is tested.
+ Workers are stateless pods behind an HPA; the queue is observable (`/runs/{id}/jobs`, `/platform/workers`).
− Polling adds latency (`SDTF_WORKER_POLL_SECONDS`); a broker-backed queue can replace `claim_job` later behind the
  same functions. Finalisation currently runs inside one worker; splitting transform/load into jobs is the next step.

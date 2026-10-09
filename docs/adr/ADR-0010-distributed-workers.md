# ADR-0010: Claim-based distributed extraction workers with leases
**Status:** Accepted

## Context
Extraction must scale horizontally and survive worker loss. A message broker (Kafka/Redis) is an operational
dependency not every SAP landscape team wants on day one.

## Decision
Jobs live in the metadata database (`extraction_jobs`: one per partition *and stage* EXTRACT / TRANSFORM / LOAD).
`sdtf worker` processes claim a job with a conditional UPDATE (exactly one winner), hold a lease, process the
partition against the shared staging backend and mark the job DONE. Expired leases are re-queued; attempts are
counted. **Pipelined mode (default):** when a job finishes, its successor for the same partition is enqueued at
once, so partitions flow through extraction, transformation and load independently; workers prefer later-stage
jobs to drain the pipeline. **Barrier mode:** the next stage's jobs are enqueued only when the previous stage is
complete. In both modes a stage closes with aggregated metrics once every partition is through it, under an
ATOMIC RUNNING -> ADVANCING transition so exactly one worker closes it; after LOAD the same worker runs
reconciliation and the report. Lost wake-ups (a job finishing while another worker holds the lock) are covered by
re-evaluation after the lock is released and by idle workers, which also enqueue missing successors
(`repair_pipeline`) so a crash between finishing a job and enqueueing its successor cannot stall a run. Load jobs are concurrency-safe: they check
only their partition's keys and insert with conflict-ignore semantics, re-reading rows that lost an insert race.
INLINE execution (threads in the API process) remains for small scopes.

## Consequences
+ No broker; works on SQLite in development and PostgreSQL in production; crash recovery is tested.
+ Workers are stateless pods behind an HPA; the queue is observable (`/runs/{id}/jobs`, `/platform/workers`).
− Polling adds latency (`SDTF_WORKER_POLL_SECONDS`); a broker-backed queue can replace `claim_job` later behind the
  same functions. Reconciliation and the report still run inside the advancing worker (distributed per-table reconciliation
  is the next step). Pipelining relaxes stage ordering only per partition; cross-partition de-duplication still
  happens at staging-write time, and reconciliation tolerates identical copies staged by two partitions.

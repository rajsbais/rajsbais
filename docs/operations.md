# Operations guide

## Deployment
See `docs/09-repository-deployment-architecture.md`. Images: `deploy/Dockerfile.backend` (runs `alembic upgrade head`
then uvicorn), `deploy/Dockerfile.frontend` (nginx serving the built SPA, proxying `/api`).

## Database migrations
Alembic (`backend/alembic.ini`, `backend/sdtf/migrations`). `alembic upgrade head` on deploy; `alembic revision
--autogenerate -m "…"` after model changes. SQLite for development, PostgreSQL in compose/Kubernetes.

## Runbooks
* **Failed migration run**: open Extraction & Load Monitor → select run → inspect FAILED stage metrics (`error`) →
  fix cause → *Resume from checkpoint* (completed extraction partitions are skipped). Exceptions: Data Quality Dashboard.
* **Reconciliation not PASS**: Reconciliation Center → filter FAIL/WARN → *Explain variances* → act on
  root-cause category (SCOPE_POLICY / TRANSFORMATION / LOAD). Technical PASS does not clear financial sign-off.
* **Manifest cannot be approved**: Carve-out Studio → Pending business dispositions → decide per object or bulk.
* **Audit chain broken** (`/audit/verify` ok=false): treat as a security incident; the event id in `broken_at`
  identifies the first inconsistent record; restore from backup and investigate write access to `audit_events`.
* **Rotate auth secret**: change `SDTF_AUTH_SECRET`; all tokens are invalidated (users re-login).

## Distributed runs
Start runs with `execution=DISTRIBUTED`; scale `sdtf worker` processes/pods as needed. Monitor `/runs/{id}/jobs`
and `/platform/workers`. A worker crash leaves a CLAIMED job whose lease expires (`SDTF_JOB_LEASE_SECONDS`); any
worker re-queues it on its next poll. `POST /runs/{id}/jobs/requeue` re-queues FAILED jobs after fixing the cause.
Extraction, transformation and load each run as one job per partition; reconciliation runs as one technical job
per staged table plus a functional and a financial job once every load job is done. In pipelined mode (default) a partition's
next-stage job is queued as soon as the previous one finishes; `pipelined=false` on run start restores stage
barriers. A stage closes automatically when its last job completes; idle workers also close stages and re-create
missing successor jobs, so a stalled run normally heals itself within one poll interval. A run stuck in ADVANCING means the advancing worker died mid-transition: `POST /runs/{id}/resume`
re-queues the open stage's failed or orphaned jobs and the workers continue.

## Backups
PostgreSQL (metadata, manifests, audit) and the evidence directory/bucket are the two stateful components. Evidence
files are content-addressed via `evidence_index.json`; verify hashes after restore.

## Observability
`/healthz`; per-stage metrics in `run_stages`; audit events for every governance action. Planned: OpenTelemetry.

## Housekeeping
`staged_records` grows per run; retain runs that back sign-offs, purge the rest by `run_id` after the evidence
package is archived.

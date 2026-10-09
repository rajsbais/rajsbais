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
* **Enable single sign-on** (ADR-0012): register SDTF in the identity provider as a public client with redirect URI
  `https://<ui-host>/auth/callback` and post-logout URI `https://<ui-host>/`, PKCE S256 required, authorization
  code grant (+ refresh if wanted), groups in the token. Set `SDTF_OIDC_ISSUER`, `SDTF_OIDC_AUDIENCE` (= client id
  unless the access token carries a different audience, then also `SDTF_OIDC_CLIENT_ID`), `SDTF_OIDC_JWKS_URL`,
  `SDTF_OIDC_ROLE_MAP`, optionally `SDTF_OIDC_SCOPES`, `SDTF_OIDC_GROUPS_CLAIM`, `SDTF_OIDC_TENANT_CLAIM`. Endpoints
  come from `<issuer>/.well-known/openid-configuration` (override with `SDTF_OIDC_DISCOVERY_URL` or the explicit
  `SDTF_OIDC_AUTHORIZATION_ENDPOINT` / `SDTF_OIDC_TOKEN_ENDPOINT` / `SDTF_OIDC_END_SESSION_ENDPOINT`). The API must reach
  the token endpoint; the UI must be served over https (or localhost). `SDTF_OIDC_CLIENT_SECRET` only for confidential
  clients; `SDTF_OIDC_EXCHANGE=browser` if the provider must be called from the browser instead. Check
  `GET /api/v1/auth/oidc/config` (reports `error` when discovery fails) and set `SDTF_DEV_USERS=0`.
* **Connect a real SAP source over RFC** (ADR-0013): have Basis import the add-on (`sap-abap/src/`, reviewed and
  activated by an ABAP developer), create a technical user with `S_RFC` for FUGR `ZSDTF` and `S_TABU_NAM` 03 for the
  tables in scope, install the SAP NW RFC SDK + `pyrfc` in the API/worker image, set `SDTF_RFC_DEST_<SID>` (JSON
  `pyrfc.Connection` parameters, e.g. `{"ashost":"…","sysnr":"00","client":"100","user":"SDTF_READ","passwd":"env:ECP_PW"}`
  or SNC parameters) and the referenced secret, register the system with connector `RFC`, then run
  `POST /systems/{id}/connector/test`: it reports transport, snapshot, T001 metadata and a verified 5-row package, or
  the exact error (`RFC_UNAVAILABLE`, `NOT_AUTHORIZED`, `SNAPSHOT_*`, `CHECKSUM_MISMATCH`, logon/communication
  errors). Tune `SDTF_RFC_PACKAGE_SIZE` (default 5000, add-on cap 10 000) and `SDTF_RFC_KEY_CHUNK` to the source's
  work-process budget. Without a system, `meta.rfc.transport = "simulated"` runs the same code path on synthetic data.
* **Connect a real S/4HANA target over its released APIs** (ADR-0015): activate the OData services the bindings use
  (API_BUSINESS_PARTNER, API_PRODUCT_SRV, API_SALES_ORDER_SRV, API_PURCHASEORDER_PROCESS_SRV,
  API_OUTBOUND_DELIVERY_SRV, API_COSTCENTER_SRV, API_PROFITCENTER_SRV) and the journal entry SOAP service, create a
  communication user or OAuth client with the matching scopes, expose the `YY1_` extension fields the bindings emit,
  set `SDTF_S4_API_<SID>` (`{"base_url":"https://…","user":"SDTF_LOAD","passwd":"env:S4_PW"}` or
  `token_url`/`client_id`/`client_secret`), register the target with connector `API`, run
  `POST /systems/{id}/connector/test` (CSRF token fetched, nothing written). Compare every binding with the
  service's `$metadata` before the first delta cycle; `REJECTED_BY_TARGET` events carry the service's message.
* **Load mode**: runs load through the released APIs by default (`SDTF_LOAD_MODE=api`, per run `load_mode`); the LOAD
  stage metrics show calls per service and operation, assigned keys and the cockpit share. `load_mode=direct` writes
  the simulated target directly (comparison runs only). On an HTTPS target, cockpit objects (histories, billing
  documents, G/L accounts, assets, material documents, invoice receipts, custom tables) are refused with the reason:
  export them instead.
* **Migration cockpit staging files**: after a run, `POST /runs/{id}/cockpit-export` (or the *Export cockpit files*
  button on the Runs page, or `sdtf cockpit-export --run <id>`) writes the rows the initial load routed to the
  cockpit under `<SDTF_EVIDENCE_DIR>/cockpit/<run>/`: one CSV per staging table, one SpreadsheetML workbook per
  migration object (Introduction, Field List, one sheet per table), `manifest.json` with a sha256 per file,
  `README.md` with the upload steps, and a zip served by `GET /runs/{id}/cockpit-export/download`. The package is
  rewritten on every export and recorded in the audit trail (`COCKPIT_EXPORTED`). The files are **not** generated
  from the target's own migration object templates (those are release specific): download the template in the
  *Migrate Your Data* app and map the columns; migration object names in the manifest are hints to verify.
* **Delta synchronisation** (ADR-0014): after a completed baseline run on an RFC source, run cycles from the Delta
  Synchronization Monitor or `POST /runs/{baseline}/delta/cycles`; each cycle is a run with CAPTURE/TRANSFORM/APPLY/
  RECONCILE stages and an event ledger (`GET .../delta/events`: FILTERED reasons, REJECTED change sets, CONFLICT
  stale events). Conflicts: inspect the event, fix the target or re-trigger the change at the source, re-run a cycle
  (the ledger never re-applies a known sequence). Before cutover an approver declares the freeze
  (`POST .../delta/freeze`), then run `{"final": true}`; the baseline is cutover-ready only when the final full
  reconciliation passes. A failed cycle leaves the watermark at the last completed cycle; the next cycle re-captures.
  Lag and backlog figures from the simulated source are not performance data.
* **SSO sign-in fails**: the callback screen shows the reason. `state mismatch` / `no login in progress`: the
  browser tab lost `sessionStorage` (private window, redirect across hosts) or the attempt is older than 10 minutes.
  `PKCE verification failed`: the provider does not support S256 or the client registration disables PKCE.
  `issuer mismatch` / `audience mismatch`: align `SDTF_OIDC_ISSUER`/`SDTF_OIDC_AUDIENCE` with the token claims
  (decode the ID token at the provider). `OIDC discovery failed`: the API cannot reach the issuer; set explicit
  endpoints. Every successful SSO login is a `LOGIN` audit event with `method: oidc`.

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
OpenTelemetry (ADR-0011): point `OTEL_EXPORTER_OTLP_ENDPOINT` at a collector; traces show `sdtf.run` → stages for
inline runs and `sdtf.job` spans (stage, partition, worker, attempt) for distributed runs, with staging writes as
children. Useful queries: p95 of `sdtf.job.duration` by stage; `sdtf.jobs` with status=FAILED; `sdtf.jobs.requeued`
(lease expiries indicate dying or hanging workers); `sdtf.staging.files_scanned` vs `files_pruned` (index
effectiveness); `sdtf.reconciliation.checks` by layer/status. Logs are JSON with `trace_id`; `/healthz` and
`/api/v1/platform/telemetry` report the active exporter. Per-stage metrics also remain in `run_stages`.

## Housekeeping
`staged_records` grows per run; retain runs that back sign-offs, purge the rest by `run_id` after the evidence
package is archived.

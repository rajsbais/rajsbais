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
* **Reconcile again after the systems moved** (or after switching to the other VM): `POST /runs/{id}/reconcile` or
  `sdtf reconcile --run <id>` re-runs the three layers through the adapters and refreshes the report; the summary names
  the read path per side (`record_store`, `rfc_addon`, `rfc_aggregate`, `api_readback`), the mode decision and the
  tables not verified. `mode=aggregate` keeps the line items in the systems: totals computed in the source, and in the target when it
  hosts the add-on (`SDTF_RECON_MODE`, `SDTF_RECON_AGGREGATE_ABOVE`, `SDTF_RECON_MAX_ROWS`).
* **Cutover rehearsal**: Cutover Command Center → *New rehearsal* (mock / dress / go-live) → refresh the automatic
  items, tick the manual ones with notes, *Start* and time the runbook tasks, record lessons → an approver closes it
  with GO (refused while a blocking item is open) or NO-GO; the report is the cutover binder entry
  (`sdtf cutover-rehearsal`, docs/cutover-rehearsal.md).
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
  from the target's own migration object templates (those are release specific); migration object names in the
  manifest are hints to verify.
* **Template-driven export**: download the migration object's XML template from the *Migrate Your Data* app and
  register it for the project (`POST /projects/{id}/cockpit-templates` with the file content, the Runs page's
  template upload, or `sdtf cockpit-template register --project <id> --object <BO> --file <xml>`). The template is
  parsed (Field List, technical-name row of each data sheet) and mapped automatically: same technical names,
  BAPI-style aliases (`COMP_CODE` -> `BUKRS`), parent keys for item sheets; the report lists coverage and the
  mandatory fields still unmapped. Record overrides per sheet (`PUT .../mapping`, or `--mapping file.json`:
  `{"<sheet>": {"table": "VBRP", "fields": {"FIELD": "VBRP.NETWR" | "=constant" | ""}}}`). The next export writes
  `<OBJECT>.template.xml`: the template itself with rows below each technical-name row, dates as DateTime cells,
  amounts and quantities as Number cells, keys as strings (leading zeros kept); styles and header rows are
  preserved. Illustrative samples (`sdtf cockpit-template sample --object FI.GLAccount`, also in
  `docs/cockpit-templates/`) show the layout SAP documents; they are not SAP files. Check a downloaded template
  before registering it: `sdtf cockpit-template check --file <download.xml> --object <BO>` (or
  `POST /cockpit-templates/check`) reports whether the documented layout was recognised (8 header rows, key span
  in row 7, hidden SAP Field column) and names every assumption otherwise; the Runs page shows the verdict per
  template. Filled files are line oriented and go through SAP's own XML file splitter (see
  docs/cockpit-template-validation.md).
* **Migration object IDs per release**: the files of every cockpit object are labelled with the migration object
  they are for, resolved for the target's release (`SapSystem.release`, e.g. `2023`, `S/4HANA 2023 FPS01`,
  `cloud`): the project's registry first, then the catalogue of documented objects (`catalog/migration_objects.py`:
  names per release with their renames Vendor -> Supplier, Material -> Product, availability such as "Sales order
  (open)" from 1709; technical `SIF_*` IDs are hints marked *unverified*). Accounting documents resolve per staging
  table (BSID -> receivable, BSIK -> payable, BKPF/BSEG -> G/L balance and open item). Make it authoritative by
  importing the target's object list: `POST /projects/{id}/migration-objects/import` with
  `[{name, id, release, object_types, tables}]`, `sdtf migration-objects import --project <id> --file list.json`,
  or the paste box on the Runs page; `sdtf migration-objects list --release 2023` prints the catalogue and
  `--project <id>` the project's resolution. The export manifest and README carry name, ID, source and confidence.
* **Upload simulation feedback**: after uploading the files, the app simulates every instance and produces a
  message log. Import it (`POST /runs/{id}/cockpit-feedback/import` with the file content, the upload box on the
  Runs page, or `sdtf cockpit-feedback import --run <id> --file log.csv`; CSV/TSV with any delimiter, JSON or the
  spreadsheet export as SpreadsheetML XML; columns recognised by several spellings). Messages are matched to the
  exported instances by instance key (any separator, with or without leading zeros), by the object's key columns
  when the log carries them, or by a key search when the migration object is not named; what cannot be placed is
  reported as unmatched with the reason. Instances with an error are marked `COCKPIT_ERROR` in staging (lineage
  kept) and get COCKPIT-stage exceptions; the summary on the run report classifies the errors (configuration
  missing, mandatory field missing, duplicate, format/value, authorisation, locked, other) and gives the pass rate
  per object. Fix the data, mapping or configuration, then `POST /runs/{id}/cockpit-export` with
  `scope=rejected` (or `sdtf cockpit-export --scope rejected`) for a retry package of the rejected instances only
  (`<run>-retry/`). `DELETE /runs/{id}/cockpit-feedback` clears the feedback and resets the statuses. An
  illustrative log for a run (`GET /runs/{id}/cockpit-feedback/sample`, `sdtf cockpit-feedback sample`) exercises
  the flow; it is not an SAP file, and the app's real log format has not been seen here.
* **Package rounds (re-upload tracking)**: every cockpit export is a *round* (`GET /runs/{id}/cockpit-rounds`,
  `sdtf cockpit-feedback rounds --run <id>`, the rounds table on the Runs page): round 1 is the full package,
  later rounds are retry packages of the instances still rejected. A round is `EXPORTED`, then `UPLOADED` once
  someone records the upload in the app (`POST /runs/{id}/cockpit-rounds/{n}/mark` with a note such as the app
  project and transfer id, `sdtf cockpit-feedback mark --round n --status UPLOADED --note ...`), `SIMULATED` once
  the simulation log is imported against it (the default target of an import is the latest round not yet
  simulated; `round` picks another), and `MIGRATED` once the app's migration step is recorded; a package
  exported before the previous one was simulated supersedes it. The import records an outcome per instance of
  the round (accepted, rejected, not in log), releases instances that were rejected earlier and are accepted now
  (staging back to `LOADED`, lineage kept), and the burn-down shows per round how many instances were retried,
  accepted, rejected and resolved, the instances still rejected with their round history and last messages, and
  whether the rounds converged. `scope=rejected` exports always take the instances whose latest outcome is
  rejected. Clearing the feedback forgets the outcomes but keeps the rounds and their upload marks.
* **Template field names (aliases)**: a template field resolves by its own DDIC name, then by a *project alias*,
  then by the global catalogue of BAPI-style names (`catalog/fields.py`, from the public BAPI structures:
  `COMP_CODE`, `PSTNG_DATE`, `AMT_DOCCUR`, `MOVE_TYPE`, ...), then by its Field List description matching the
  DDIC description of a field of the sheet's table. Names nothing resolves are *proposed* from the Field List
  when a template is registered (`sdtf cockpit-template aliases --project <id>`, `GET
  /projects/{id}/cockpit-aliases`, the aliases table on the Runs page); global aliases whose DDIC description
  disagrees with the Field List are proposed for confirmation too. Confirm or reject each (`--confirm all`,
  `POST .../cockpit-aliases/{id}/decide`), or add one by hand (`POST .../cockpit-aliases`). Confirmed aliases
  apply to every template of the project; the mapping report labels each field `direct`, `project_alias`,
  `alias`, `described`, `parent`, `related`, `override`, `constant` or `unmapped`, so every heuristic is visible.
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

# Roadmap tracker: what is still pending and how it will be built

Generated from `backend/rfactory/roadmap.py` (do not edit by hand: `python -m rfactory.roadmap --write docs/07-roadmap-tracker.md`). The same list is on the **Roadmap** screen.

Status: **needs-you** waits on something only you can do · **ready** can be built now without a real SAP system · **after** waits on another item · **declined** deliberately not built.  Size: S under a day, M a few days, L a week or more.

27 items: 5 needs-you, 13 ready, 1 declined, 8 after.

## P1 · Look at it and try it on your machine

_Nothing here needs more building: it needs you to run it._

| ID | Item | Owner | Status | Size | Waits on |
|---|---|---|---|---|---|
| R1 | First run on your PC | you | needs-you | S | - |
| R2 | Read-only smoke test against NPL (ECC) | both | needs-you | S | R1 |
| R3 | Read-only smoke test against A4H (S/4HANA) | both | needs-you | S | R2 |
| R10 | Real identity-provider test of the browser login | you | needs-you | S | - |

### R1 · First run on your PC

- **Needs:** Python 3.11+ and Node.js 20+ on the machine (not on the NPL/A4H VM unless you want to)
- **Plan:**
  1. Run `python start.py` (Windows: double-click start.bat; this path has never been run on Windows)
  2. Control tower > Load synthetic landscape, then Guided refresh
  3. Try Data analysis and the Selective designer with a project, a person other than you approving, and Reconciliation
  4. Tell me what looked wrong or confusing
- **Done when:** You have seen a refresh released end to end and have a list of anything that looked wrong.

### R2 · Read-only smoke test against NPL (ECC)

- **Needs:** NPL running (A4H stopped: only one runs at a time), a read-only SAP user, pyrfc and the SAP NetWeaver RFC SDK installed where the platform runs, the password in an environment variable
- **Plan:**
  1. Landscape > Connect a real system: fill the form, tick the sandbox/read-only confirmation, run the smoke test
  2. The test reads a few rows per table and never writes; its report holds no row values
  3. Send me the .md report (docs/04-real-system-test.md has the details)
- **Done when:** A smoke report from NPL with no unexplained BLOCKER lines.

### R3 · Read-only smoke test against A4H (S/4HANA)

- **Needs:** NPL stopped and the A4H appliance in the docker-host VM running; same prerequisites as R2; port 8000 is NPL's, the platform uses 8088
- **Plan:**
  1. Same as R2 against A4H, over RFC and, if enabled, OData
  2. Send the report
- **Done when:** A smoke report from A4H.

### R10 · Real identity-provider test of the browser login

- **Needs:** A test identity provider (Keycloak, Entra ID, Okta) with an app registration for a public client
- **Plan:**
  1. Register the app with the redirect URI of the platform
  2. Set the RFACTORY_OIDC_* variables (README)
  3. Sign in, sign out, and try a replayed or expired login; send me the result
- **Done when:** Authorization Code + PKCE login works against a real provider, or the failure is known.

## P2 · Build what needs no real SAP system

_Can start now and run in parallel while you do P1._

| ID | Item | Owner | Status | Size | Waits on |
|---|---|---|---|---|---|
| B14 | Security review pass and supply-chain scanning in CI | me | ready | M | - |
| B2 | PostgreSQL: connection pool, reconnect and failure drills | me | ready | M | - |
| B3 | Schema migrations for the state store | me | ready | M | - |
| B7 | Refresh tokens, silent renewal and token introspection | me | ready | M | - |
| B13 | UI: theme switch, in-app help and tour, remaining polish | me | ready | M | R1 |
| B15 | Operations: TLS reference setup and runbook | me | ready | M | - |
| B1 | Delete data tool | me | declined | M | - |

### B14 · Security review pass and supply-chain scanning in CI

- **Needs:** Nothing
- **Plan:**
  1. Run a full security review of the API, auth, masking and persistence code and fix what it finds, each with a test
  2. Add dependency vulnerability scanning and an SBOM to CI
  3. Add a secret scan and a container image scan (the image itself is still unbuilt, see R8)
- **Done when:** Findings fixed or recorded with a reason; CI fails on a known-vulnerable dependency.

### B2 · PostgreSQL: connection pool, reconnect and failure drills

- **Needs:** Nothing (a local PostgreSQL is available for tests)
- **Plan:**
  1. Reconnect and retry a request when the database connection drops mid-request, without losing the write lock's guarantees
  2. A small pool for reads
  3. Automated drills: database restart, killed connection, killed instance
  4. Document what is guaranteed in each case
- **Done when:** Drill tests pass; no lost or duplicated change in any drill.

### B3 · Schema migrations for the state store

- **Needs:** Nothing
- **Plan:**
  1. Version every aggregate shape and write forward migrations
  2. Refuse and explain when a database is newer than the program
  3. Test upgrading databases saved by earlier versions of this repository
- **Done when:** An old state database opens, is migrated, and keeps every project and run.

### B7 · Refresh tokens, silent renewal and token introspection

- **Needs:** Nothing to build; R10 to prove it against a real provider
- **Plan:**
  1. Use refresh tokens where the provider issues them and renew before expiry
  2. Introspect opaque tokens
  3. Keep the revocation list authoritative
- **Done when:** A session outlives the access token without re-login; revoked tokens stop at once.

### B13 · UI: theme switch, in-app help and tour, remaining polish

- **Needs:** Your notes from R1 decide what to polish first
- **Plan:**
  1. A light/dark/auto switch that remembers your choice
  2. A short guided tour and a help panel on each screen
  3. Fix what you found in R1
  4. A screen-reader review needs a person: I will prepare the script, you or a colleague runs it
- **Done when:** You can find and use every feature without being told where it is.

### B15 · Operations: TLS reference setup and runbook

- **Needs:** Nothing
- **Plan:**
  1. A reverse proxy with TLS reference (Caddy or nginx) that fits the compose files
  2. Runbook: backup and restore of the state and its keys, key rotation, upgrade, incident steps
  3. Health and metrics endpoints suitable for a monitor
- **Done when:** A new operator can run, back up, upgrade and recover it from the document alone.

### B1 · Delete data tool

- **Needs:** Declined: you asked that no data be deleted
- **Plan:**
  1. Not built. The tile stays greyed out as 'Not built yet' in Solutions. Say so if you ever want it.
- **Done when:** Not applicable.

## P3 · Make it true on real systems

_Starts when the smoke-test reports from NPL and A4H arrive._

| ID | Item | Owner | Status | Size | Waits on |
|---|---|---|---|---|---|
| R4 | Check the table and field models against the real reports | me | after | M | R2, R3 |
| B10 | Name mapping for the EWM tables (/SCWM/...) | me | after | S | R4 |
| R7 | Verify the OData mappings on A4H | me | after | M | R3 |
| R5 | Compile and test the ABAP loader in an NPL sandbox | both | after | M | R2 |
| R6 | First real selective refresh into a sandbox | both | after | M | R4, R5 |

### R4 · Check the table and field models against the real reports

- **Needs:** The smoke reports
- **Plan:**
  1. Compare each reported table and field with the platform's model for QM, PM, PS, WM, HR, flight and the OData mappings
  2. Fix names, lengths and keys that differ
  3. Record in the capability matrix which models were confirmed on a real system
- **Done when:** Every model is marked confirmed or corrected.

### B10 · Name mapping for the EWM tables (/SCWM/...)

- **Needs:** The real EWM table names from A4H
- **Plan:**
  1. Map the SCWM_* stand-ins to the /SCWM/ tables at the connector boundary
  2. Test against the fake and the real report
- **Done when:** An EWM refresh reads the real tables.

### R7 · Verify the OData mappings on A4H

- **Needs:** The A4H smoke report with OData enabled
- **Plan:**
  1. Compare each mapped entity and property with the service metadata
  2. Fix gaps; mark what the OData source cannot supply
- **Done when:** The OData source reads sales orders correctly or says what it cannot.

### R5 · Compile and test the ABAP loader in an NPL sandbox

- **Needs:** A sandbox client in NPL you may write to, developer rights, the ABAP source in docs/abap
- **Plan:**
  1. Import and activate ZRF_LOADER and its tables; switch it on in ZRF_CFG for one table
  2. Run ZRF_PING from the platform
  3. Send me every syntax or runtime error; I fix and you re-run
  4. Replace the number-range update with the standard number range APIs
  5. Test upsert, delete and rollback on a dummy table
- **Done when:** The loader passes its handshake and a write/rollback test in the sandbox.

### R6 · First real selective refresh into a sandbox

- **Needs:** A source and a separate writable sandbox target (for example two NPL clients)
- **Plan:**
  1. Read-only plan from the real source
  2. Approve with a second person
  3. Run into the sandbox and read the reconciliation
  4. Roll back and confirm the target is as before
- **Done when:** A released refresh and a clean rollback on a real system.

## P4 · Larger pieces

_Worth doing once P1 to P3 show what matters most._

| ID | Item | Owner | Status | Size | Waits on |
|---|---|---|---|---|---|
| R8 | Build and run the container files; two instances behind a proxy | you | needs-you | M | B15 |
| B5 | Durable orchestration | me | ready | L | B2 |
| B6 | ABAP extraction agent for high-volume reads | me | after | L | R5 |
| B8 | More business objects | me | ready | L | R4 |
| B9 | Maintenance and project extensions | me | ready | L | R4 |
| B11 | Data analysis: archiving-object links and aggregation at the source | me | ready | M | R4 |
| B12 | Multivariate benchmark model and scheduled re-calibration | me | ready | M | R6 |
| B4 | Key management service providers | me | ready | M | - |
| B17 | Test-data connectors and a durable catalog | me | ready | L | B3 |
| B18 | Delta refresh hardening | me | after | L | R6 |
| B16 | Real full-refresh and post-copy adapters | me | after | L | R6 |

### R8 · Build and run the container files; two instances behind a proxy

- **Needs:** Your docker-host VM
- **Plan:**
  1. Build the image
  2. Run docker-compose.postgres.yml with two instances
  3. Kill one instance mid-request and watch the other
  4. Send me anything that fails
- **Done when:** Both compose files run and survive the drill.

### B5 · Durable orchestration

- **Needs:** Nothing to build (Temporal or Argo is a product decision for you)
- **Plan:**
  1. Move the in-memory queue to the database
  2. A clock-driven worker with leases enforced inside every module
  3. Parallel workers with throttling
  4. Real notification channels
- **Done when:** Jobs survive restarts and run on time without a person clicking.

### B6 · ABAP extraction agent for high-volume reads

- **Needs:** A working loader (R5) so the ABAP side can be compiled
- **Plan:**
  1. ABAP source that reads and packages large tables on the SAP side
  2. Platform adapter and a fake for tests
  3. Throughput test on a real system
- **Done when:** A large table is read without the RFC_READ_TABLE limits.

### B8 · More business objects

- **Needs:** Confirmed models (R4) make this safer
- **Plan:**
  1. Batches, stock and valuation
  2. Payroll results and time infotypes (HR)
  3. Work-centre master
  4. Key remapping (REMAP) for number clashes
  5. Each with data, checks, tests and a screen entry
- **Done when:** Each object refreshes and reconciles like the existing ones.

### B9 · Maintenance and project extensions

- **Needs:** Confirmed models
- **Plan:**
  1. Task lists and maintenance plans
  2. Order components and costs
  3. Networks, activities, budgets and WBS assignment of orders
- **Done when:** Planned maintenance and project cost flows refresh whole.

### B11 · Data analysis: archiving-object links and aggregation at the source

- **Needs:** Real archiving-object definitions from a system
- **Plan:**
  1. Map tables to SAP archiving objects
  2. Push the grouping to the source instead of reading every row
- **Done when:** Analyses run on large tables without a row cap.

### B12 · Multivariate benchmark model and scheduled re-calibration

- **Needs:** Real timings from R6
- **Plan:**
  1. Model row width, index load and parallelism
  2. Re-fit on a schedule and flag drift
- **Done when:** Estimates track real runs within the stated interval.

### B4 · Key management service providers

- **Needs:** A cloud account to prove it for real
- **Plan:**
  1. Providers for AWS KMS and Azure Key Vault behind the existing key interface
  2. Tested against fakes; real proof is yours
- **Done when:** The data key is wrapped by a managed key.

### B17 · Test-data connectors and a durable catalog

- **Needs:** Access to Cloud ALM, Jira or Xray to prove it
- **Plan:**
  1. Connectors and outbound webhooks
  2. More templates (PP, FI-AA, MM-IM, intercompany)
- **Done when:** A test request raises and closes a ticket automatically.

### B18 · Delta refresh hardening

- **Needs:** A real change-document log
- **Plan:**
  1. Per-class mapping from configuration
  2. A scheduler daemon and parallel packages
- **Done when:** A weekly delta runs unattended on a real system.

### B16 · Real full-refresh and post-copy adapters

- **Needs:** Access to SWPM, HANA tools and VM snapshots
- **Plan:**
  1. Adapters for the copy tools
  2. Real adapters for the post-copy tasks
- **Done when:** A system copy and its post-copy run from the platform in a sandbox.

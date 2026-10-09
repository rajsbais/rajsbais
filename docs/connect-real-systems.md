# Connecting the real ECC and S/4HANA systems, one at a time

This is the procedure for the first contact with real systems installed on your own machine. The platform never
needed SAP credentials to be built; everything so far ran on the synthetic landscape and the simulated gateway.
Each step below says what it touches on the SAP side and what the platform reads back. Nothing in this procedure
writes to SAP until step 5, and step 5 is yours to start.

> The platform has to run on a machine that can reach the SAP systems (the same PC, or one on their network). A
> cloud session cannot reach them. Secrets are never stored in the platform's database: destinations reference
> environment variables (`env:NAME`), and the registration refuses passwords in the request body.

## Your landscape: NPL and A4H on Hyper-V, one at a time

Two VMs on the Windows host (Hyper-V switch `SAP-Net`), only one running at a time because of memory:

| VM | What it is | What it can play |
|---|---|---|
| `SAP-NPL-15.2` (SID **NPL**, instance 00, client 001) | SAP NetWeaver AS ABAP 7.52 developer edition on openSUSE Leap 15.2: **basis only** (SAP_BASIS / SAP_ABA, Flight model). No ECC application layer: no FI/SD/MM transactions, no business data in BKPF/BSEG/VBAK/MARA (the tables of SAP_APPL are not installed) | the **add-on bench**: create and test the `Z_SDTF_*` function modules, prove the RFC contract (connector test, discovery). It cannot be the ECC source of a carve-out: there is nothing to carve out |
| **A4H** (S/4HANA fully activated appliance, client 100) | full S/4HANA application with demo company codes (1010, 1710, ...), Fiori launchpad (`https://vhcala4hci...:44300`), released OData/SOAP services, the *Migrate Your Data* app | **source and target**: source through the same `Z_SDTF_*` add-on (A4H allows Z development), target through the released APIs and the migration cockpit |

So the honest plan is not "ECC first, S/4HANA second" but:

1. **NPL up**: build the add-on (`sap-abap/`), run the connector test and discovery against NPL. Expect T001 with at most the
   delivery company code 0001 and no business objects; what this step proves is the RFC path (SDK, destination, authorisations,
   the four function modules, snapshot token, checksums). Keep the transport request: the same objects go into A4H.
2. **A4H up, as source**: create the add-on in A4H (or import the transport), register A4H a second time with role SOURCE and
   connector RFC, discover it (real company codes, plants, sales organisations, table statistics), design a scope on one demo
   company code, run with the target **simulated**: extraction over RFC with real volumes, transformation, cockpit export.
3. **A4H up, as target**: register A4H with role TARGET and connector API, connector test, download the migration object
   templates from its *Migrate Your Data* app and check them (`sdtf cockpit-template check`), import its object list, upload
   a package and import the simulation log. A real API load into A4H only against a copied client (SCCL), never client 100.

A run that needs the other VM fails at that stage with a connection error (EXTRACT needs the source, LOAD the target);
switch VMs and *Resume from checkpoint*: completed stages and completed extraction partitions are skipped.

> **Reconciliation reads through the adapters** (ADR-0016): the source side over the add-on (company-code pushdown;
> `Z_SDTF_AGGREGATE` proves the read complete, so create it with the other modules), the target side back through the
> released APIs (entities by key, filtered collections, `API_JOURNALENTRYITEMBASIC_SRV` for journal entries). Asset values
> come from `API_FIXEDASSET` and material valuation from `A_ProductValuation`; tables with no read path (T001, T001K)
> are reported as *not verified* (WARN), never as a false FAIL. The journal item property
> names are unverified against A4H's `$metadata`: send the first `$metadata` of that service if the read fails. After
> switching VMs, `POST /runs/{id}/reconcile` (or `sdtf reconcile --run`) re-runs the reconciliation without repeating the
> load. On a large company code use `?mode=aggregate` (`--mode aggregate`): the totals are computed in the source and only
> the retained documents' lines are transferred; `auto` switches to it above `SDTF_RECON_AGGREGATE_ABOVE` line items.

Networking: the platform runs on the Windows host (Python, the SAP NW RFC SDK for Windows, `pyrfc`; if no `pyrfc` wheel
exists for your Python, use a 3.12 virtual environment for the API process) or inside the `docker-host` VM through
`deploy/docker-compose.yml` with the SDK mounted. The VMs' hostnames (`vhcalnplci`, `vhcala4hci`) must resolve from where the
platform runs (hosts file), the RFC gateway port is 3300 for instance 00 on both, and A4H's HTTPS port is 44300; its
self-signed certificate must be in the machine's trust store.

## 0. Prerequisites on your machine

| Need | Why | How |
|---|---|---|
| Python 3.13, Node 20 | run the platform | `cd backend && pip install -e ".[dev,columnar]"`; `cd frontend && npm install` |
| `pyrfc` + SAP NW RFC SDK | the ECC adapter speaks RFC through `pyrfc` | install the SDK from the SAP Support Portal (S-user), set `SAPNWRFC_HOME`, then `pip install pyrfc`; `python -c "import pyrfc"` must succeed |
| The `Z_SDTF_*` function modules in ECC | the extraction contract (`sap-abap/README.md`): `Z_SDTF_OPEN_SNAPSHOT`, `Z_SDTF_TABLE_METADATA`, `Z_SDTF_READ_PACKAGE`, `Z_SDTF_CDC_POLL` | create them from `sap-abap/src/*.abap` and the structures in `sap-abap/DDIC.md` in a development client, mark them remote-enabled, transport to the client you read from; they are read-only |
| An RFC service user in ECC | authorisation for the four function modules and for reading the tables in scope | S_RFC for the function group, S_TABU_DIS/S_TABU_NAM for the tables; no change authorisations |
| Released OData/SOAP services activated in S/4HANA | the loaders use `API_BUSINESS_PARTNER`, `API_PRODUCT_SRV`, `API_SALES_ORDER_SRV`, `API_PURCHASEORDER_PROCESS_SRV`, `API_OUTBOUND_DELIVERY_SRV`, `API_COSTCENTER_SRV`, `API_PROFITCENTER_SRV`, `API_JOURNALENTRYITEMBASIC_SRV`, `API_FIXEDASSET` (read) and the SOAP service `JournalEntryBulkCreateRequestConfirmation_In` | `/IWFND/MAINT_SERVICE` (OData), SOAMANAGER (SOAP); a communication user with the services' authorisations; HTTPS reachable from your machine |
| A **sandbox client** on S/4HANA for step 5 | the API loaders post real documents | never point step 5 at a productive client |

## 1. Start the platform locally

```bash
cd backend
export SDTF_DATABASE_URL=sqlite:///./data/sdtf.db SDTF_EVIDENCE_DIR=./data/evidence SDTF_STAGING_DIR=./data/staging
sdtf init-db
sdtf serve --port 8000
# second terminal
cd frontend && npm run dev          # http://localhost:5173, sign in as architect / architect
```

Create a project on the **Portfolio** page (or `POST /api/v1/projects`). Keep the demo project separate; it uses
the synthetic systems.

## 2. ECC first: register and test the RFC connector (reads only)

Put the destination in the environment of the API process, never in the database:

```bash
export SDTF_RFC_DEST_ECP='{"ashost": "127.0.0.1", "sysnr": "00", "client": "100", "user": "SDTF_RFC", "passwd": "env:ECP_RFC_PASSWD", "lang": "EN"}'
export ECP_RFC_PASSWD='...'
export SDTF_RFC_TRANSPORT=pyrfc          # auto would still pick pyrfc when a destination exists
```

Register the system with connector `RFC` on the Portfolio page (SID `ECP`, client `100`, role SOURCE, product
ECC, release `6.0 EHP8`), or:

```bash
curl -s -X POST http://localhost:8000/api/v1/projects/<project>/systems -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sid": "ECP", "client": "100", "role": "SOURCE", "product": "ECC", "release": "6.0 EHP8", "connector": "RFC", "meta": {"rfc": {"transport": "pyrfc"}}}'
```

Then **Test RFC connector** on the Landscape page (`POST /api/v1/systems/<id>/connector/test`). What it does on
ECC, in this order, and what you should see:

1. `Z_SDTF_OPEN_SNAPSHOT`: a consistency token comes back (`snapshot` in the result).
2. `Z_SDTF_TABLE_METADATA` for `T001`: key fields and field list match the catalogue.
3. `Z_SDTF_READ_PACKAGE` for `T001`, package of 5 rows: rows come back and their checksum matches.
4. `Z_SDTF_AGGREGATE` for `T001`: the row count computed in the database (`aggregate.available`); when the module is
   missing the test still passes and says so, and the reconciliation reads rows without integrity evidence.

A failure names the step and the RFC error (`RFC_COMMUNICATION_FAILURE`, missing function module, missing
authorisation). Fix it on the SAP side and test again; nothing was written.

Then run **discovery** (Landscape page, `POST /api/v1/systems/<id>/discover`): it reads release, organisational
units, table statistics and object counts through the same function modules. Compare the company codes, plants
and sales organisations it shows with what you expect.

## 3. Scope and transform against ECC data (still no writes anywhere)

Design a scope on the **Scope Designer** page for one company code, preview the impact, create and approve the
manifest, generate and approve a ruleset. Then start a run with the target still **simulated**: register the
S/4HANA system with connector `API` and `meta.api.transport = "simulated"` (step 4 below), so the run extracts
from the real ECC over RFC, transforms, loads into the simulated gateway and reconciles. Check:

* EXTRACT metrics: `adapter=RFC`, partitions, records per second, `snapshot_id` equal to the one the connector test
  returned;
* RECONCILE: the three layers PASS or explained; the source side now comes through RFC (watch the `source_read_integrity` /
  `source_read_amounts` rows, which compare the rows read with totals computed in the source), the simulated target side
  from the gateway;
* the cockpit export: the staging files for the cockpit objects, now carrying real data.

Start with a small company code or a date-bounded scope: the first extraction measures the real throughput.

## 4. S/4HANA second: register and test the API connector (reads only)

```bash
export SDTF_S4_API_S4P='{"base_url": "https://s4.local:44300", "user": "SDTF_API", "passwd": "env:S4P_API_PASSWD"}'
export S4P_API_PASSWD='...'
# or OAuth2 client credentials: {"base_url": "...", "token_url": "...", "client_id": "...", "client_secret": "env:S4P_CLIENT_SECRET"}
export SDTF_S4_API_TRANSPORT=auto        # http when a destination exists; set "simulated" to stay on the gateway
```

Register the target with connector `API` (SID `S4P`, role TARGET, product S4HANA, release as installed, e.g.
`2023`); the release drives the migration object lookup. **Test API connector** on the Landscape page: it fetches
a CSRF token from `API_BUSINESS_PARTNER` and probes the configuration (company codes); nothing is written. A
self-signed certificate must be trusted by the machine's certificate store, or the base URL must be reachable
through a reverse proxy with a trusted certificate.

Register the migration object templates of your release on the Runs page (download them from the *Migrate Your
Data* app), check each with `sdtf cockpit-template check --file <download.xml> --object <BO>`, confirm the alias
proposals, and import the app's object list (`sdtf migration-objects import`). Everything up to here is verified
against the documented layout only; your downloads are the first real templates the parser sees, so send me the
check output if it reports deviations.

## 5. The first real load, on the sandbox client

Switch the target's `meta.api.transport` to `http` (or remove the override with `auto`) and start a run with
`load_mode=api` on a **small scope**. The loaders then post through the released APIs: business partners,
products, open sales and purchase orders, deliveries, journal entries (with the source reference kept on the
entry so a re-run finds them instead of posting twice). Watch the LOAD stage metrics (`api.by_operation`,
`failures`, `REJECTED_BY_TARGET` exceptions with the service's message) and the reconciliation.

Cockpit objects are not posted over HTTPS: export the package, upload it in the *Migrate Your Data* app, run the
simulation there, import the message log (Runs page), fix what it rejects, export the retry package, and track
the rounds until they converge.

## 6. What to send back after each step

* the connector test result (JSON),
* the discovery summary and the first run's stage metrics,
* `sdtf cockpit-template check` output for each downloaded template,
* the first simulation log the app produced (the import's column recognition has only seen illustrative logs).

Each of these is something the platform could not verify without a system; with them the remaining "verified on
the simulator only" notes in `docs/capability-status.md` can be closed one by one.

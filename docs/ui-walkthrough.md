# UI walkthrough: migration cockpit export, templates, aliases and migration objects

Screenshots taken from the running application on the demo landscape (RFC connector, simulated S/4HANA gateway),
signed in as the `architect` dev user. Everything shown is simulated: no SAP system is involved.

## Run it yourself

```bash
# backend (Python 3.13): demo landscape + API on :8000
cd backend
pip install -e ".[dev,columnar]"
export SDTF_DATABASE_URL=sqlite:///./data/sdtf.db SDTF_EVIDENCE_DIR=./data/evidence SDTF_STAGING_DIR=./data/staging
sdtf demo --connector RFC          # synthetic landscape, approved manifest and ruleset, one completed run
sdtf serve --port 8000

# frontend (Vite) on :5173, proxied to the API
cd frontend && npm install && npm run dev
```

Open http://localhost:5173, sign in with `architect` / `architect` (dev users: admin, architect, approver,
operator, auditor, viewer; passwords equal the names), select the demo project and open **Runs**.

## The flow

1. **Sign in** (`cockpit-01-login.png`): dev login form; with `SDTF_OIDC_*` set, the SSO button starts the PKCE flow.
2. **Runs page** (`cockpit-03-runs-top.png`): the completed run with its stages (EXTRACT, TRANSFORM, LOAD through
   the released APIs, RECONCILE PASS, REPORT).
3. **Migration cockpit staging files** (`cockpit-04-cockpit-export.png`): *Export cockpit files* writes the package
   (CSV per staging table, SpreadsheetML workbook per object, manifest, README, zip) and lists every object the
   initial load routed to the cockpit with its migration object, resolved for the target release (2025): a
   DOCUMENTED pill for catalogue entries, NONE for objects without a standard migration object, CANDIDATES when
   the staging table decides (accounting documents). Below it, the **Migration objects of the target release**
   table and the paste box to import the app's object list.
4. **Templates** (`cockpit-07-alias-proposal.png`): *Use illustrative sample* registers a template in the layout
   SAP documents; the table shows sheets, mapped fields, mandatory fields still unmapped, the layout check
   verdict and the number of alias proposals. The billing sample names one field its own way (`INVOICED_QTY`),
   so one alias is proposed from its Field List description.
5. **Aliases** (`cockpit-08-alias-confirmed.png`): the proposal is confirmed; it now resolves as `project_alias`
   for every template of the project.
6. **Re-export** (`cockpit-09-reexport-with-templates.png`): the package now also carries `<OBJECT>.template.xml`
   for the two registered templates (39 files), and *Download zip* fetches it.
7. **Upload simulation feedback** (`cockpit-12-simulation-feedback.png`, in the same card, below the export): import the app's message log or use the
   illustrative sample; rejected instances are marked, errors classified, and *Export retry package* writes the
   rejected instances only.
8. **Package rounds** (`cockpit-13-package-rounds.png`, same card): each export is a round; mark uploads and migrations by hand, and follow the
   burn-down of the still-rejected instances across retry rounds.
9. **Delta monitor** (`cockpit-10-delta-monitor.png`): delta cycles through the same loaders, for context.
10. **Metadata check** (Landscape page, API targets): reads the gateway catalogue and the `$metadata` of every bound
    service and reports the bindings' deviations per entity set (docs/metadata-verification.md).
11. **Cutover rehearsal checklist** (`cutover-14-rehearsal-checklist.png`, Cutover Command Center): a mock cutover
    created from the runbook; automatic items evaluated from the platform state, manual items ticked by hand, task
    timings and the approver's GO / NO-GO.
12. **Read configuration** (`landscape-15-read-configuration.png`, Landscape page, RFC sources and API targets): how
    the reconciliation reads the selected system over the add-on: journal table and ledger, the asset chain (FAAV_ANLC,
    APC line items by movement category, net postings) and the inventory chain (MBEW through the Material Ledger proxy
    view, CKMLCR period totals with period and currency type, inventory accounts). Saved through
    `PUT /systems/{id}/read-config` (validated, audited; transport and destination untouched, no secret passes); the
    effective values with their defaults are shown above the form, and the Reconciliation page's *Read path* card shows
    the measures the last reconciliation used.

The full-page capture of the cockpit card is `cockpit-04-cockpit-export.png`; the other images are viewport
captures at 1440x900.

## The Transform Factory shell (eleven sections)

The front end follows the Transform Factory requirement screens: a dark navy sidebar on wide screens (eyebrow
*TRANSFORM FACTORY*, the project name, eleven primary sections with icons, the project picker and the signed-in user at
the bottom), a top bar with a horizontally scrolling section row below 1000 px (`factory-27-narrow.png`), a warm light
workspace, an honesty banner on every page that says what the project's systems really are (synthetic landscape and
simulated gateway, or the connected hosts), big monospace figures in the stat tiles, pill-shaped segmented controls and
full-width primary actions. Sections with several applications show them as a second row of pills.

| Section | Page (`factory-*.png`) | What it shows | Built on |
|---|---|---|---|
| Dashboard | Transformation journey (28) | the platform's own five phases (discover and scope, analyse and design, transform and simulate, execute and cut over, govern and sign off) with every deliverable's status read from the project's state, the validated outputs, and the three S/4HANA approaches (system conversion, new implementation, selective data transition) with what the platform does in each and which one the project uses; a phase strip on the executive dashboard links to it | discovery, graph, manifests, rule sets, runs, completeness, rehearsals, delta state, evidence, approvals, audit verify |
| Dashboard | Executive dashboard (16) | company codes, plants, sample objects and database size of the source; *Quick carve-out* per company code with related objects, shared risk and cross-company documents evaluated by the scope engine; recent runs; capability status | discovery snapshot, `POST /projects/{id}/scopes/evaluate` |
| Landscape | Landscape explorer (17) | company codes (name, country, currency, parent / spin role), plants (company code, valuation area), business objects with instance counts; the analyzer, organisational structure and catalog as sub-pages | org structure, business-object inventory |
| Carve-out | Carve-out studio (18) | company code and shared-object policy, *Generate scope and analyze* writes a versioned, hashed manifest; the immutable manifest card; dispositions, detections, classification, completeness and residual exposure below; scope designer, Bluefield and merger as sub-pages | manifests, carve-out services |
| Dependencies | Dependency graph (19) | related objects and the shared / cross-company ones within two hops of a company code; the neighbourhood explorer, traversal and relationship model below | graph neighbourhood and traversal |
| Connect | System connections (20) | source RFC destination (host, system number, client, user, transport), target OData destination (base URL, client, user, TLS), password only as an environment-variable reference, *Test ECC / S/4HANA handshake*; discovery, metadata check and the read configuration | `GET/PUT /systems/{id}/destination`, connector test |
| Extract | ABAP extraction (21) | *Run extraction agents* (needs an approved manifest and ruleset), the tables the latest run extracted with rows and the agent that read them, the checkpoint; run monitor and data quality as sub-pages | runs, EXTRACT stage metrics |
| Rules | Transformation rules (22) | the rules of the current set as one line each, dry-run on a source sample with *Source lines* and *Preview*, approval; the YAML editor, validation and lineage below | rule sets, dry run |
| CDC | Near-zero downtime (23) | delta documents, backlog, stage k/9 and the nine ordered stages derived from the baseline run and the delta cycles; *Advance stage* runs the next real action (delta cycle, freeze, final delta) | delta state and cycles |
| Finance | Financial reconciliation (24) | the reconciliation center with the read path and the measures used on S/4HANA | reconciliation |
| Cutover | Production cutover (25) | gates from the selected rehearsal: automatic items as Blocked / Passed / Failed rows, manual items with *Mark done*, *Authorize production handover* refused while a blocking gate is open; runbook, risk and the full rehearsal below | cutover rehearsals |
| Audit | Audit report (26) | the verified vertical slice of a run: verdict, timestamp, company code, related objects, manifest hash, shared exposure, source / target / matched record counts, reconciliation status, evidence files and approvals; compliance and evidence as a sub-page | run report, reconciliation, evidence |

The journey is an original phase model: nothing in it is asserted by hand, and the approach names are SAP's own. Shared risk and shared exposure are the share of a scope's objects that are shared, referenced or need a manual
disposition (LOW below 10 %, MEDIUM below 30 %, HIGH above; `frontend/src/lib.ts`, unit-tested). The verdict
`VERTICAL_SLICE_COMPLETE` means the run completed and no reconciliation check failed; it does not claim a production
migration.

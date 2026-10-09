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

The full-page capture of the cockpit card is `cockpit-04-cockpit-export.png`; the other images are viewport
captures at 1440x900.

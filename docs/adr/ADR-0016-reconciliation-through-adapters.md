# ADR-0016: Reconciliation reads source and target through the adapters

Status: accepted (2026-10-09). Supersedes the record-store-only reconciliation for real systems.

## Context
The three-layer reconciliation (technical, functional, financial) compared the run's staging with two
`RecordStore` views loaded from the platform's own `sap_records` table. That is the simulated landscape: the
synthetic source and whatever the simulated gateway wrote. With a real source over RFC (`pyrfc`) and a real target
over HTTPS, both views were empty, so RECONCILE could not say anything about real systems (noted before the first
NPL / A4H test, docs/connect-real-systems.md).

## Decision
`reconcile_run` keeps its two store-shaped inputs; where they come from is decided per connector
(`backend/sdtf/reconciliation/views.py`):

* **Source over RFC**: the financial tables the checks read (T001K, BKPF, BSEG, BSID, BSIK, ANLC, MBEW) are read
  through the add-on with the scope's company codes (valuation areas for MBEW) pushed down, under one snapshot
  token. A new read-only function module `Z_SDTF_AGGREGATE` (COUNT / SUM per group, computed in the source
  database) sizes every read first (`SDTF_RECON_MAX_ROWS` guard) and provides the debit/credit totals per
  company code; both are compared with what arrived and recorded as TECHNICAL checks `source_read_integrity`
  and `source_read_amounts`. A truncated or inconsistent read therefore fails the reconciliation instead of
  passing unnoticed. An add-on without the module still serves rows (no evidence). The simulated add-on takes
  the same path, so the simulated runtime now exercises the real code.
* **Target over the released APIs**: loaded records are read back by key through the entity bindings; tables
  with a company code property are read as filtered collections (`$filter=CompanyCode eq ...`, paged with
  `$top`/`$skip`); journal entries come from `API_JOURNALENTRYITEMBASIC_SRV` and yield the header, line and
  open-item images the checks expect; masters referenced but not loaded are fetched lazily. Tables no released
  read service covers in this build (T001, T001K, ANLC, and anything without a binding) are reported as
  `unreadable`; the checks that need them say so with WARN instead of failing on an empty table, and the run
  summary lists them under `not_verified`.
* Record-store systems (SYNTHETIC, simulated gateway) keep the direct read: the simulated gateway writes the
  record store, so that is what "the API" holds; results on the demo landscape are unchanged.
* `POST /runs/{id}/reconcile` and `sdtf reconcile --run` re-run RECONCILE + REPORT on a completed run through
  the adapters: the way to reconcile after the other VM came up, or after the target moved.

## Consequences
* Reconciliation against real systems is now possible, with its own read-integrity evidence; it is verified on
  the simulated add-on and gateway and on a mocked HTTPS endpoint, not on a live SAP system.
* The journal item property names and the open-item derivation come from the public API reference; what the
  references confirm and what only A4H's `$metadata` can is recorded in `docs/metadata-verification.md`, and
  `sdtf metadata check` runs that verification against a target or a downloaded EDMX.
* Asset values are read back through the fixed-asset read service (`API_FIXEDASSET` / `FixedAssetValuation`)
  and material valuation through the product valuation entity (`API_PRODUCT_SRV` / `A_ProductValuation`,
  filtered by the valuation areas the source's T001K maps to); both are read-only bindings in
  `catalog/api_bindings.py`, written from the public API reference and unverified against a target's
  `$metadata`. When a service is not activated or refuses, the table falls back to the **RFC read-back**: an
  on-premise target that also hosts the read-only add-on (`meta.rfc` on the target system) serves the tables no
  released API covers (T001, T001K, ANLC, loaded history tables such as EKBE or VBFA) through
  `Z_SDTF_READ_PACKAGE` with the target company codes or the loaded keys pushed down, and `Z_SDTF_AGGREGATE`
  counts as `target_read_integrity` evidence. Only a target without the add-on leaves those tables *not
  verified* (WARN).
* Reading a whole company code's BSEG for reconciliation is a real transfer on large systems. The
  **aggregate-only mode** (`SDTF_RECON_MODE=aggregate`, or `auto` above `SDTF_RECON_AGGREGATE_ABOVE` line items,
  default 1 000 000; `?mode=` on the re-reconcile endpoint) keeps the line items in the source: GL balances per
  account, open-item counts and sums, asset and inventory values and open intercompany balances are computed
  there by `Z_SDTF_AGGREGATE`; only T001K and the line items of the documents the scope *retains* (read by key,
  bounded by the classification) cross the wire; the "rejected" bucket comes from the staging and the
  "filtered" bucket is the remainder (lines never extracted). The verdicts equal the row read's on the demo
  landscape; two things are weaker and said so in the results: document-currency totals are compared per company
  code (`cc/*`) because a line carries no document currency, and lines of partially transferred documents that were
  not extracted count as "not extracted" instead of "unexplained". The read-integrity evidence becomes
  `source_trial_balance` (debits equal credits per company code, computed in the source). On the target, aggregate
  mode needs the read-only add-on there (`meta.rfc`): the journal line items are then not read back through
  `API_JOURNALENTRYITEMBASIC_SRV` at all; GL balances, open items, asset values, inventory values and open
  intercompany balances are computed in the target by `Z_SDTF_AGGREGATE`, with `target_trial_balance` as
  evidence. The technical layer then says the loaded journal rows were compared as totals (WARN, not a false
  FAIL), the per-document balance is not verified (the target's posting logic enforces it, and the explanation
  says so), and the accounting-document status comparison is not verifiable. `auto` decides per side by that
  side's journal size. Without the add-on on the target, aggregate mode is refused there and the APIs read rows.
  On an **S/4HANA** system (source or target) the journal aggregates come from the Universal Journal **ACDOCA**
  (one ledger, `meta.rfc.ledger`, default 0L; `meta.rfc.journal_table` forces ACDOCA or BSEG): GL balances per
  account, debit/credit totals, open items (account type D/K without clearing document) and open intercompany
  balances are aggregated there with the signed amounts normalised to the debit/credit form the checks use;
  document counts still come from BKPF and inventory from MBEW. Asset acquisition values on S/4HANA come from a chain
  (`meta.rfc.assets`): the compatibility view **FAAV_ANLC** (the classic ANLC figures reproduced from the line
  items, SAP note 2270387: same semantics as ECC, compared PASS/FAIL), else the **APC line items** of ACDOCA
  (depreciation areas that post to the general ledger) and FAAT_DOC_IT (statistical and non-posting areas)
  filtered by depreciation area and the movement categories that carry acquisition and production costs
  (`apc_movement_categories`, taken from the FAA_MOVCAT domain on the system: none are assumed), else the net
  asset postings of the Universal Journal, a different measure reported as WARN with the measure named. ECC-type systems, and S/4HANA systems whose ACDOCA the add-on
  cannot read or that hold no rows, keep BSEG and the open-item tables.
  **Inventory values on S/4HANA** (RFC read-back and aggregate mode, `meta.rfc.inventory`): the Material Ledger is
  mandatory there and MBEW's LBKUM/SALK3 are no longer updated in the table; Open SQL reads of MBEW are served by the
  proxy view `MBV_MBEW`, which computes them from the Material Ledger, and that is what the add-on's dynamic SELECT
  gets (a database-level read shows them empty). Chain: MBEW through the proxy view (SALK3 by valuation area,
  comparable with ECC), else, when MBEW returns valuated materials with every stock value zero or cannot be read, the
  Material Ledger period totals `CKMLCR` (valuation area through `CKMLHD`, one period and currency type, default the
  calendar month of the read and currency type 10: the same measure, comparable; set `period` for non-calendar
  fiscal years or a closed period), else the balance of the configured `inventory_accounts` in the Universal
  Journal by plant (valuation area = plant on S/4HANA), a different measure reported as WARN with the measure
  named; nothing configured and nothing readable is reported as not readable, never as a FAIL. In rows mode the MBEW
  rows read back through the add-on are then compared on prices only (LBKUM/SALK3 excluded) and the totals come from
  the chain. Verified on the simulated add-on with Material Ledger records derived from the classic rows
  (`material_ledger_from_mbew`); the proxy-view behaviour of A4H is a public-reference claim to confirm on the VM.
  All of it (journal table, ledger, asset chain, inventory chain) is managed per system through
  `GET/PUT /systems/{id}/read-config` (`reconciliation/read_config.py`: validated, audited, only the four read keys
  of `meta.rfc` change, transport and destination untouched) and the *Read configuration* card of the Landscape page;
  the Reconciliation page's *Read path* card shows the measures the last reconciliation used.

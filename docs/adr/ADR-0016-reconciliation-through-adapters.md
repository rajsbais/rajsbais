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
* The journal item property names and the open-item derivation come from the public API reference; they must
  be checked against the target's `$metadata` on A4H (unverified, like the other bindings).
* Asset values and material valuation are not verifiable through the APIs in this build (WARN, by report);
  adding read services for them is backlog.
* Reading a whole company code's BSEG for reconciliation is a real transfer on large systems; the aggregate
  guard refuses reads above the limit, and aggregate-only reconciliation (totals compared without rows) is the
  next step for very large scopes.

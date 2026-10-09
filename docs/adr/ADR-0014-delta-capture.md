# ADR-0014 — Delta capture and replay through the add-on contract

**Status:** accepted

## Context
After the initial load the source keeps changing; near-zero-downtime migrations replay those changes until a
business freeze, then run a final delta and reconcile (docs/07, stages 4-10). A generic log reader cannot carry
SAP semantics (ADR-0001), so capture happens in the SAP add-on (`Z_SDTF_CDC_POLL`) and the platform owns ordering,
scope, idempotency and reconciliation.

## Decision
1. **Capture contract.** `Z_SDTF_CDC_POLL(watermark, objects, package)` returns table-row events with a monotonic
   `SEQ`, a `CHANGENR` grouping the rows of one business change, `OP` I/U/D, timestamp, user and the current row
   image; delivered in sequence order, checksummed, with an opaque watermark that the dropped events also advance.
   The simulated add-on implements it over a persisted change log; the ABAP reference (`sap-abap/src/z_sdtf_cdc_poll.abap`)
   maps it to CDHDR/CDPOS, timestamp watermarks and table logging. The watermark of a run starts at the value the
   snapshot handed out (`EV_CDC_WATERMARK`), so initial load and delta share one consistency point.
2. **A delta cycle is a run** (`MigrationRun` of kind DELTA bound to a completed baseline) with its own stages:
   PRECHECK → CAPTURE → TRANSFORM → APPLY → RECONCILE → REPORT. It reuses the baseline's approved manifest and
   ruleset; nothing unapproved can reach the target through the delta path.
3. **Scope at capture time.** Header tables of company-code-owned objects are subscribed with the manifest's
   company codes pushed down; item tables are subscribed unfiltered and decided per header: manifest membership
   (transfer classes in, excluded/retained out), organisational test on the current header image for objects
   created after the snapshot, and an org-view test for new client-level masters. Filtered events are kept in the
   ledger with their reason.
4. **Atomic change sets, deterministic order, idempotent apply.** Transformation rejects a whole change set when
   one of its rows fails a rule. Apply order is configuration → masters → documents in DOC_FLOW/ACCOUNTING_REF
   topological order, then change time and sequence. Each event is recorded in `delta_events` keyed by
   (baseline, sequence): a re-run ignores known sequences; identical content is a no-op; an older sequence than
   the last one applied to the same target key is a CONFLICT and leaves the target untouched; deletes of rows never
   loaded are skipped.
5. **The baseline's staging follows.** Applied inserts/updates/deletes are mirrored into the baseline run's staging
   so its three-layer reconciliation (technical checksums, functional chains, financial balances) stays meaningful
   after any number of cycles.
6. **Freeze and final delta.** An approver declares the business freeze on the baseline (audited); the simulated
   source refuses changes for frozen company codes. The final cycle requires the freeze, ends with the full
   reconciliation of the baseline against the current source and target (document statuses refreshed from the
   source), and marks the baseline cutover-ready when it passes. No further cycles are accepted afterwards.
7. **Business activity is simulated honestly.** `simulate-changes` plays documents and masters created, changed and
   deleted after the snapshot into the source record store and its change log. It is the stand-in for a living
   SAP system and is refused for real systems.

## Consequences
* Stages 4-10 of the NDT model run end to end on synthetic data; their behaviour is tested, their performance is
  not: no downtime, lag or backlog figure from this build describes a real landscape (docs/benchmarks.md).
* The target remains the simulated record store; a real target needs the API/migration-cockpit loaders of ADR-0007
  to accept deltas (inserts and updates through released APIs, deletes as reversal postings where SAP forbids
  deletion).
* The ABAP reference for `Z_SDTF_CDC_POLL` is not compiled or tested on an SAP system; change-document coverage,
  timestamp granularity and table logging differ per release and must be validated there.
* Delta cycles run inline (one API call); distributing capture per table is a later step if volumes require it.

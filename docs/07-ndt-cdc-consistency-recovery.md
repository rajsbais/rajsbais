# 07 — Near-zero-downtime (NDT), CDC consistency and recovery design

**Status: SIMULATED for stages 1–10 (ADR-0014).** Delta capture goes through the SAP add-on contract
(`Z_SDTF_CDC_POLL`, reference ABAP not compiled) over the RFC adapter and is exercised against the simulated add-on;
replay, freeze, final delta and the final full reconciliation are implemented in `runtime/delta.py`. Stage 11 is the
runbook's go/no-go gate (partial), 12–13 are human stages. No downtime figure is claimed anywhere in the product; the
cutover forecast is template-based and labelled as such, and simulated cycles say nothing about real lag or backlog.

## What runs today
* `POST /systems/{id}/simulate-changes` plays business activity (new/changed/deleted sales orders, FI documents,
  customer masters, across company codes) into a simulated source and its change log.
* `POST /runs/{baseline}/delta/cycles` runs a delta cycle: CAPTURE (poll after the watermark, scope filter with
  reasons) → TRANSFORM (baseline ruleset, change sets rejected atomically) → APPLY (config → masters → documents in
  DOC_FLOW/ACCOUNTING_REF order; insert / update / delete; idempotency ledger `delta_events`; stale events are
  CONFLICTs) → RECONCILE (every applied event re-read from the target) → REPORT. The baseline's staging is kept in
  step so its three-layer reconciliation stays valid.
* `POST /runs/{baseline}/delta/freeze` (approver) declares the business freeze; the simulated source then refuses
  changes for the frozen company codes. `{"final": true}` runs the final delta and the full reconciliation of the
  baseline (document statuses refreshed from the source) and marks the baseline cutover-ready when it passes.
* `GET /runs/{baseline}/delta` shows watermark, cycles, freeze, readiness and the backlog still waiting at the source
  (events, in-scope share, lag). The Delta Synchronization Monitor drives all of it.

## Stage model
1 Initial extraction → 2 Initial transformation → 3 Initial target load → 4 Delta capture → 5 Delta transformation →
6 Continuous synchronisation → 7 Backlog monitoring → 8 Business freeze coordination → 9 Final delta synchronisation →
10 Final reconciliation → 11 Cutover authorisation → 12 Business validation → 13 Production handover.

## CDC adapters are source-specific, by design
A generic database log reader cannot reproduce SAP business semantics (cluster/pool tables on older releases,
number-range buffering, document-level atomicity across BKPF/BSEG/BSID, archiving, change documents). Planned
adapters per source:
* **ECC on AnyDB**: application-level change capture via change pointers / CDHDR-CDPOS for masters and table-log or
  timestamp watermarks (AEDAT/UDATE, CPUDT) for documents, read through the add-on (`Z_SDTF_CDC_POLL`).
* **ECC/S4 on HANA**: trigger-based or SLT-compatible change tables where licensed; ODP delta queues for CDS extractors.
* **Deltas are captured at business-object granularity** (document = header + all items in one change event) so
  replay respects referential dependencies.

## Ordering, idempotency, replay
* Change events carry (object type, key, source change sequence, snapshot/watermark). Apply order = topological order
  of DOC_FLOW/ACCOUNTING_REF edges, then sequence.
* Transformation is deterministic (doc 05); load is an idempotent upsert keyed by target key with content comparison
  (already implemented: `skipped_duplicate` / `updated_idempotent` / `conflicts`).
* Duplicate detection: a replayed event with identical content is a no-op; different content with the same key is a
  conflict unless the event sequence is newer.

## Restart and recovery (implemented for the initial load)
* Partition checkpoints (`run_stages.checkpoint.partitions_done`) make `resume_run` skip completed partitions.
* Stage states DONE/FAILED are persisted; a failed TRANSFORM resumes without re-extraction (tested).
* Recovery checkpoints for cutover: the runbook marks the point of no return (T10) after which forward recovery
  (fix in target, replay delta, re-reconcile) replaces rollback.

## Benchmarks (required before any NDT claim)
`docs/benchmarks.md` holds the measurement protocol: records/s per adapter, source CPU/IO impact, delta lag under
load, backlog drain rate, recovery time, and the resulting migration-window fit computed by the Migration Performance
Agent. Simulated numbers (≈20k rec/s in-memory at scale 1) are explicitly not representative.

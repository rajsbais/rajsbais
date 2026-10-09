# ADR-0015 — Loads through released S/4HANA APIs (delta cycles and initial load)

**Status:** accepted

## Context
ADR-0007 forbids direct writes into the target's application tables. The initial simulated load tags every
record with the load method the S/4 compatibility registry prescribes but still writes the record store directly;
delta cycles (ADR-0014) did the same. Replaying changes on a real target means calling its released APIs with
their rules: deep inserts, ETag-guarded updates, no deletion of masters or accounting documents, numbers assigned
by the target, derived prices and statuses.

## Decision
1. **A target API contract with two transports.** Loaders depend only on `ApiTransport.request(method, service,
   path, payload, headers)`. `S4ApiHttpTransport` speaks to a real system (OData V2 JSON under
   `/sap/opu/odata/sap/<service>/`, CSRF handshake, basic or OAuth2 client-credentials auth, SOAP envelope for
   `JournalEntryBulkCreateRequestConfirmation_In`). `SimulatedS4Gateway` implements the same services over the
   target's record store and is the executable contract: CSRF tokens, ETags/If-Match (412), OData keys and error
   shapes, configuration validation (company codes, sales organisations, plants), derived fields (item re-pricing
   at the unit price, header totals, document status, billing company code from the sales organisation), deep
   inserts with cascade on delete, duplicate detection (409), balanced-entry and company-code checks for journal
   entries, target-side numbering, reversal postings, open items derived from customer/supplier lines, block flags
   for masters. It persists through `sap_records`, so reconciliation sees exactly what the API accepted.
2. **Bindings per business object** (`catalog/api_bindings.py`): service, protocol, header and item entity sets,
   field-to-property maps, key properties, derived (never sent), priced (sent on create, recomputed afterwards),
   updatable, parent properties, numbering (external/internal), delete policy (DELETE / BLOCK / REVERSAL /
   FORBIDDEN), history rules. Unmapped fields travel as `YY1_` extension properties and their share is reported, so a
   partial mapping is visible, not hidden. Bindings are written from the public API reference and must be checked
   against the target's `$metadata`.
3. **The loader turns a change set into API operations.** One business change (CHANGENR) of one instance is one
   load: deep insert for new documents, PATCH of updatable properties with the ETag read just before, POST/PATCH/
   DELETE of items, DELETE with cascade, BLOCK for masters, reversal (+ re-posting) for accounting documents. Keys
   the target assigns (journal entries by default) are written back to the ledger and the baseline's staging so
   every later check compares against what the target holds. Objects without an API path end as `UNSUPPORTED`
   with the reason (migration-cockpit-only, history, no binding, direct-table), configuration objects are
   `MATCHED` or `CONFIG_MISSING`, and API rejections are `REJECTED_BY_TARGET` with the service's message.
4. **Engine pre-checks stay.** Duplicate content and stale sequences are decided from the ledger before any call;
   the delta reconciliation re-reads the last image per target key (a document changed twice in one cycle counts
   once) and checks reversals and blocks for what they are.
5. **Targets are registered with the `API` connector** (`meta.api.transport = simulated | http`, destination from
   `SDTF_S4_API_<SID>` with secrets as `env:NAME`, passwords refused in the database) and have a connector test
   (CSRF token from `API_BUSINESS_PARTNER`, configuration probe, nothing written). The demo target on the RFC path
   uses the simulated gateway.

6. **The initial load uses the same loaders** (`runtime/api_load.py`, default `SDTF_LOAD_MODE=api`, per run
   `load_mode`). The LOAD stage groups transformed staging records by business object instance (open items are
   grouped by their document through the row image) and runs one load per instance: deep inserts and item creates
   for open documents, master creates with their company-code views, journal entry postings with the target's
   numbers and open items derived from the customer/supplier lines (hints carry assignment, special G/L and
   clearing data), configuration matching, and the **migration cockpit** for cockpit objects, for tables the
   document APIs do not expose (PO history, production order components/confirmations) and for **histories**:
   completed sales orders, fully delivered purchase orders and goods-issued deliveries are migrated as history, not
   re-created (their statuses cannot be set through the APIs). Keys and images the target assigns are written back
   to staging, so the three-layer reconciliation compares against what the target holds. Re-runs and concurrent
   partitions are safe: existing documents are compared (duplicate / conflict), journal entries are looked up by a
   source reference kept on the entry (`YY1_SDTF_SOURCE_REF`, read back through `API_JOURNALENTRYITEMBASIC_SRV` on a
   real target) before posting. `load_mode=direct` keeps the former simulated direct loader for comparison.

## Consequences
* Both the initial load and delta cycles now exercise the API semantics a real cutover faces, including the
  awkward ones (no deletes of masters, no edits of posted documents, numbers you do not choose, histories that
  do not go through transactional APIs). The vertical slice reconciles PASS on this path; the direct loader
  remains available but is no longer the default.
* With target-side numbering, the merge planner's disjoint number ranges keep *staging* keys apart; the target
  numbers journal entries itself and every entry carries a source reference prefixed with the sending system's
  logical system, so several sources that share company codes and document numbers cannot collide or be
  mistaken for re-runs of each other.
* Direct and API load modes must not be mixed on one target: their keys differ for target-numbered documents.
* The migration cockpit is simulated as a posting of staging-table content with the organisational checks a
  migration object performs. A real target fills staging tables through a database connection or the file-based
  app, so the HTTPS transport refuses cockpit objects with that explanation. The **staging-file export**
  (`runtime/cockpit_export.py`, `POST /runs/{id}/cockpit-export`, `sdtf cockpit-export`) writes the rows the LOAD
  stage routes to the cockpit (one decision, `plan_cockpit`, shared by loader and export) as a CSV per staging
  table and a SpreadsheetML workbook per migration object with a checksummed manifest and a zip. It is not
  generated from the target's own migration object templates, which are release specific; mapping onto them and
  the migration object IDs remain a verified step on the target.
* Pricing, statuses and open items are *modelled*, not SAP's: a real target prices from condition records and
  derives statuses from subsequent documents. The simulator's business activity is restricted to changes the
  released APIs can convey (quantities, master attributes, new documents, item deletions).
* Journal entry reversal is modelled with a reversal document that references the original; the real service's
  reversal operation and reason codes must be mapped on the target.
* Unverified here: any live S/4HANA system, OAuth scopes, the SOAP service's exact XML namespaces and
  confirmation structure, extensibility of the `YY1_` properties.

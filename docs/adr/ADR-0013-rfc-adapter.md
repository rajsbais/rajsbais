# ADR-0013 — RFC extraction adapter: transport abstraction and a simulated add-on as executable contract

**Status:** accepted

## Context
Extraction ran only against the in-platform synthetic record store. The production path (ADR-0001) is an ABAP
add-on exposing RFC-enabled read modules (`sap-abap/README.md`). No SAP system, NW RFC SDK or credentials exist in
the development environment, and none should.

## Decision
1. **One extractor, two row sources.** `ManifestExtractor` owns planning (partition = business object type ×
   owning company code) and the carve-out semantics (organisational views of shared masters, cross-company
   documents split at the company-code boundary, reference stubs). `SyntheticStoreExtractor` reads the record
   store; `RfcExtractor` fetches each partition through the add-on and feeds the same semantics. Parity is tested:
   both produce the identical (table, key) set for the demo manifest.
2. **Predicate pushdown per package.** Header tables are read with the organisational predicate and the object
   keys as EQ ranges (chunked); partitions above a size threshold push the organisational predicate only and
   filter keys client-side. Item tables are read by header-key ranges. T001K is read once. Metrics report calls,
   packages, rows transferred and the pushdown mix so source impact is visible per run.
3. **Transport abstraction.** `RfcTransport.call(function, **params)` with RFC parameter names. `PyRfcTransport`
   binds to SAP's NW RFC SDK through `pyrfc` (serialised on one connection, ABAP exceptions mapped to `RfcError`
   with the exception key). `SimulatedAbapAddon` implements the function modules in Python over a record store.
4. **The simulated add-on is the executable contract.** It enforces what the ABAP implementation must: snapshot
   token required and expiring, `S_TABU_NAM`-style allow-list, range-table predicate semantics, primary-key order,
   keyset cursor bound to table/predicate/snapshot, server-side package cap, SHA-256 package checksum over the
   transmitted row JSON. The ABAP reference sources in `sap-abap/src/` mirror it line for line but are not compiled.
5. **Secrets never in the database.** Destination parameters come from `SDTF_RFC_DEST_<SID>` or `meta.rfc.dest`
   with secrets referenced as `env:NAME`; the API rejects registrations that embed a password and masks
   destinations in responses.
6. **Connector test endpoint** proves connectivity, authorization and the contract with one snapshot, one metadata
   call and one five-row package of T001.

## Consequences
* The whole pipeline (extract → transform → load → reconcile) runs on the RFC path today via the simulated
  add-on (`sdtf demo --connector RFC`), so adapter regressions are caught without SAP.
* What remains unverified: the ABAP sources on a real release, pyrfc behaviour against a live system, SNC, RFC
  throughput and work-process budgets. These need a customer development system and are listed as such in
  `docs/capability-review.md`.
* Keyset pagination means a package read is O(package) regardless of offset, but requires a primary key; tables
  without one (none in scope) would need a different cursor.
* CDC (`Z_SDTF_CDC_POLL`) is deliberately not part of this increment; the simulated add-on raises
  `NOT_IMPLEMENTED` for it.

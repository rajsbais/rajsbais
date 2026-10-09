# 02 — Source and target SAP connectivity design

**Status: synthetic and RFC adapters implemented; the RFC adapter is verified against the simulated add-on, not
against a live SAP system (ADR-0013). OData/CDS/File remain contracts.** See `backend/sdtf/runtime/adapters.py`
and `sap-abap/README.md`.

## Principles
* Use SAP-supported mechanisms only; never bypass application semantics or SAP authorization to gain speed.
* Least privilege: dedicated technical users per system and per purpose (read, load), authorizations limited to
  the add-on's RFC modules and the tables in scope.
* Every adapter exposes `snapshot()`, `partitions()`, `extract(partition)` with checkpoint/restart semantics.
* Source impact is bounded by package size, worker budget and a records-per-second cap (`max_rows_per_second`).

## Source adapters
| Adapter | Mechanism | Fit | Status |
|---|---|---|---|
| SYNTHETIC | in-platform record store | development, CI, demos | SIMULATED |
| RFC | ABAP add-on `Z_SDTF_READ_PACKAGE` (keyset package cursor, consistency token, server-side predicate pushdown, per-package checksum) via `pyrfc` or the simulated add-on (`runtime/rfc.py`) | bulk transactional history on ECC | IMPLEMENTED (unverified against a live system) |
| CDS / ODP | CDS views with company-code/fiscal-year parameters, ODP delta queues | S/4 sources, high-volume tables, delta | PLANNED |
| ODATA | released APIs | master data and open documents, low volume | PLANNED |
| FILE | SAP-exported files (e.g. archive extracts) | one-off historical loads | PLANNED |

### RFC adapter (implemented)
`RfcExtractor` plans partitions from the approved manifest exactly like the synthetic adapter and, per partition,
reads the header table with the organisational predicate (`BUKRS EQ <owner>`) plus the object keys as EQ ranges
(chunks of `SDTF_RFC_KEY_CHUNK`, only while the partition has at most `SDTF_RFC_KEY_PUSHDOWN_LIMIT` objects; larger
partitions push the organisational predicate only and filter keys client-side), then item tables by header-key
ranges. T001K is read once per run. Every package is verified against `EV_CHECKSUM`; the run's snapshot id is the
add-on's token. Extraction metrics report calls, packages, rows transferred and the pushdown mix (`adapter_stats`).
Transports: `PyRfcTransport` (SAP NW RFC SDK through `pyrfc`, SAP-distributed, see github.com/SAP/PyRFC; not on
PyPI for current releases) and `SimulatedAbapAddon` (Python implementation of the function modules over the
record store, used by tests, `sdtf demo --connector RFC` and the connector test endpoint). Destination parameters
come from `SDTF_RFC_DEST_<SID>` / `meta.rfc.dest` with secrets referenced as `env:NAME`; the API refuses to store
passwords. SNC parameters are passed through to `pyrfc` unchanged.

### Consistency strategy
1. `Z_SDTF_OPEN_SNAPSHOT` returns a token = (DB snapshot id where available, change watermark per table, timestamp).
2. All packages of a run carry the token; the add-on rejects reads with an expired token.
3. Tables without a transactional snapshot capability (e.g. Oracle without flashback) fall back to a *watermark +
   delta* scheme: initial extraction up to watermark, then CDC delta from watermark (doc 07).

## Target loaders (S/4HANA)
**Delta loads go through the released APIs** (ADR-0015, `runtime/target_api.py`, `runtime/loaders.py`,
`catalog/api_bindings.py`): targets are registered with the `API` connector; `S4ApiHttpTransport` reaches a real
system (OData V2 under `/sap/opu/odata/sap/<service>/` with CSRF handshake, basic or OAuth2 client-credentials auth,
SOAP envelope for the journal entry service), `SimulatedS4Gateway` is the executable contract over the record store
(CSRF, ETags, configuration validation, derived pricing/status, deep inserts, target numbering, reversals, block
flags). Bindings map table fields to API properties per business object and declare derived, priced and updatable
properties, numbering and delete policy; unmapped fields travel as `YY1_` extension properties. Destination:
`SDTF_S4_API_<SID>` (JSON: `base_url`, `user`/`passwd` or `token_url`/`client_id`/`client_secret`, secrets as
`env:NAME`) or `meta.api.dest`; `SDTF_S4_API_TRANSPORT=auto|http|simulated`. The initial load uses the same
loaders (`runtime/api_load.py`, `SDTF_LOAD_MODE=api`, per-run `load_mode`): open documents through the document
APIs, journal entries with target numbering and a source reference for idempotency, histories and cockpit objects
through the (simulated) migration cockpit; `load_mode=direct` keeps the simulated direct loader. For a real target
the cockpit rows are exported as staging files (`POST /runs/{id}/cockpit-export`: CSV per table, SpreadsheetML per
migration object, checksummed manifest, zip) for the *Migrate Your Data* app, and a registered migration object
template of the release is parsed, auto-mapped (coverage report, overrides) and filled as `<OBJECT>.template.xml`.

Verify on a real target before relying on the bindings: `$metadata` of each service (property names, key order,
navigation names for deep inserts), address/role navigations of API_BUSINESS_PARTNER (flattened here), the journal
entry service's XML namespaces, reversal operation and reason codes, number range assignment per document type,
and key-user extensibility for every `YY1_` property the bindings emit.
Load strategies are chosen from the release-specific registry (`catalog/business_objects.py::S4_COMPATIBILITY`,
`BusinessObjectType.load_methods`):
* **API** — released OData/SOAP (Business Partner, Product, Sales Order, Purchase Order, Journal Entry).
* **MIGRATION_COCKPIT** — staging tables / migration objects for balances, open items, fixed assets.
* **CONFIG_TRANSPORT** — organisational configuration comes from the configuration shell; records are matched,
  not loaded (implemented in `runtime/load.py`: `MATCHED` / `CONFIG_MISSING`).
* **DIRECT_TABLE_UNSUPPORTED** — never executed; surfaced as `UNSUPPORTED` with an exception.

## Network / deployment
Hybrid connectivity: the platform runs in the customer's Kubernetes (on-prem or private cloud); SAP systems are
reached through SAP Cloud Connector / VPN; secrets (RFC credentials, API client certificates) come from the
cluster secret store (Vault / External Secrets). TLS 1.2+ everywhere; SNC for RFC.


## Reconciliation reads (ADR-0016)
The RECONCILE stage reads both sides through the same adapters the run used: the source's financial tables
through the add-on with the scope's company codes pushed down and `Z_SDTF_AGGREGATE` counts / totals as
read-integrity evidence (`source_read_integrity`, `source_read_amounts`), the target back through the released
APIs (entities by key through the bindings, `$filter`ed collections for tables with a company code property,
`API_JOURNALENTRYITEMBASIC_SRV` for journal entries, `API_FIXEDASSET` for asset values, `A_ProductValuation` for
material valuation, lazy fetch of referenced masters). Tables with no read path (T001, T001K, anything without a
binding) are reported as *not verified* and their checks carry WARN.
`SDTF_RECON_MAX_ROWS` (default 5 000 000) bounds a single table read; `SDTF_RECON_MODE=auto|rows|aggregate` and
`SDTF_RECON_AGGREGATE_ABOVE` (default 1 000 000) select the aggregate-only mode, in which the totals are computed in the
source and only the retained documents' lines are transferred. See `backend/sdtf/reconciliation/views.py`.

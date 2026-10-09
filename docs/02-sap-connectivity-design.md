# 02 — Source and target SAP connectivity design

**Status: design + contracts. Only the synthetic adapter is implemented.** See `backend/sdtf/runtime/adapters.py`
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
| RFC | ABAP add-on `Z_SDTF_READ_PACKAGE` (package cursor, consistency token, server-side predicate pushdown) | bulk transactional history on ECC | PLANNED |
| CDS / ODP | CDS views with company-code/fiscal-year parameters, ODP delta queues | S/4 sources, high-volume tables, delta | PLANNED |
| ODATA | released APIs | master data and open documents, low volume | PLANNED |
| FILE | SAP-exported files (e.g. archive extracts) | one-off historical loads | PLANNED |

### Consistency strategy
1. `Z_SDTF_OPEN_SNAPSHOT` returns a token = (DB snapshot id where available, change watermark per table, timestamp).
2. All packages of a run carry the token; the add-on rejects reads with an expired token.
3. Tables without a transactional snapshot capability (e.g. Oracle without flashback) fall back to a *watermark +
   delta* scheme: initial extraction up to watermark, then CDC delta from watermark (doc 07).

## Target loaders (S/4HANA)
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

# ADR-0007: Target loads only through released APIs, BAPIs, migration objects or configuration transports
**Status:** Accepted

## Decision
The platform never writes directly to S/4HANA application tables. Each business object declares its load methods per
target product; `DIRECT_TABLE_UNSUPPORTED` objects are surfaced as unsupported. Organisational configuration is
matched against the prepared target shell rather than loaded.

## Consequences
+ Supportability by SAP, data-model consistency (ACDOCA, MATDOC, BP) guaranteed by the application layer.
− Lower raw throughput than table-level loads; historical documents require API-based re-posting strategies.

# ADR-0006: Synthetic SAP-like landscape first; no production credentials in development
**Status:** Accepted

## Decision
A deterministic generator produces an ECC-like landscape with the patterns a carve-out engine must handle
(shared masters, cross-company documents, intercompany balances, open/cleared items, custom tables). All engine
code runs against it through the same `RecordStore` interface the SAP adapters will implement.

## Consequences
+ Reproducible tests and demos; CI exercises the full slice in seconds; no SAP licence or credentials needed.
− Synthetic data cannot validate SAP-specific edge cases (cluster tables, archiving, number-range buffering);
  every screen and report labels results as simulated until real adapters exist.

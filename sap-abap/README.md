# SAP add-on (ABAP) — interface contract, not implemented

Status: **PLANNED**. No ABAP code ships in this repository. This directory fixes the contract that the external
platform expects from an SAP-side add-on so that the extraction/CDC adapters (`backend/sdtf/runtime/adapters.py`)
can be implemented without changing the orchestration layer.

## Why an add-on at all (hybrid architecture, ADR-0001)
* Application-level consistency tokens and package cursors cannot be obtained safely through generic DB readers.
* Authorization must be enforced by SAP (S_RFC, S_TABU_NAM, S_TABU_DIS) under a least-privilege technical user.
* Source-load throttling must be enforced inside the work process (package size, parallel RFC budget).

## Read module contract (RFC-enabled)
```
Z_SDTF_READ_PACKAGE
  IMPORTING  iv_table      TYPE tabname
             it_predicate  TYPE zsdtf_t_predicate     " field / op / low / high, pushed down to WHERE
             iv_package    TYPE i                      " rows per package (throttle)
             iv_cursor     TYPE string                 " opaque, from previous call
             iv_snapshot   TYPE string                 " consistency token from Z_SDTF_OPEN_SNAPSHOT
  EXPORTING  et_rows       TYPE zsdtf_t_row            " field/value pairs, JSON-serialisable
             ev_cursor     TYPE string
             ev_eof        TYPE abap_bool
             ev_checksum   TYPE string                 " package checksum for transport integrity
```
```
Z_SDTF_OPEN_SNAPSHOT   -> consistency token (DB snapshot / timestamp + last-change watermark per table)
Z_SDTF_TABLE_METADATA  -> DDIC keys, fields, sizes, growth statistics (read-only)
Z_SDTF_CDC_POLL        -> change records since watermark (change pointers / CDHDR-CDPOS / table-log based, per object)
```
## Non-goals
* No write modules: loads go through released APIs, BAPIs or the Migration Cockpit on the target (ADR-0007).
* No direct database access that bypasses SAP semantics.

# SAP add-on (ABAP) — interface contract and reference implementation

Status: **contract fixed; platform-side RFC adapter IMPLEMENTED (`backend/sdtf/runtime/rfc.py`,
`runtime/extraction.py::RfcExtractor`); ABAP sources in `src/` are a REFERENCE IMPLEMENTATION that has not been
compiled, transported or run on an SAP system.** The executable specification of the contract is the simulated
add-on (`SimulatedAbapAddon`), which the adapter is tested against; an ABAP developer must review `src/` against
the target release before use. The platform never needs production SAP credentials to run its own tests.

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
Z_SDTF_CDC_POLL        -> change events since watermark (CDHDR/CDPOS for masters, timestamp watermarks for documents, table log for deletes)
```

## Precise semantics (what the adapter relies on, what the simulated add-on enforces)
* **Authorization**: `S_TABU_NAM` activity 03 per table → exception `NOT_AUTHORIZED`; unknown table → `TABLE_UNKNOWN`.
* **Snapshot**: every read carries `IV_SNAPSHOT` from `Z_SDTF_OPEN_SNAPSHOT`; unknown → `SNAPSHOT_UNKNOWN`, past
  `EV_VALID_UNTIL` → `SNAPSHOT_EXPIRED`. The adapter opens one token per run and restarts a partition on expiry.
* **Predicates** (`ZSDTF_T_PREDICATE`: FIELD, OP, LOW, HIGH): range-table semantics - predicates on the same field
  with positive ops (EQ BT GE GT LE LT CP) are OR-ed, negative ops (NE NB NP) are AND-ed exclusions, different
  fields are AND-ed; `CP` uses SAP patterns (`*`, `+`). Unknown op or field → `INVALID_PREDICATE`.
* **Ordering and cursor**: rows come in primary-key order; `EV_CURSOR` is opaque (base64 JSON of table, predicate
  hash, last primary key, snapshot) and valid only for the same table, predicate and snapshot → else `INVALID_CURSOR`.
  Keyset pagination, so no `OFFSET` is needed (works on NW 7.40+).
* **Package**: `IV_PACKAGE` rows per call, clamped server-side to 10 000; `EV_EOF = 'X'` on the last package.
* **Rows**: `ZSDTF_T_ROW` entries `(ROWNO, JSON)`, one JSON object per row with uppercase field names (`/ui2/cl_json`).
* **Checksum**: `EV_CHECKSUM` = lowercase hex SHA-256 over the row JSON strings exactly as transmitted, joined by
  LF. The adapter hashes the received strings before parsing and refuses the package on mismatch.
* **Change data capture** (`Z_SDTF_CDC_POLL`, reference in `src/z_sdtf_cdc_poll.abap`): `IV_WATERMARK` is opaque
  (first value: `EV_CDC_WATERMARK` of `Z_SDTF_OPEN_SNAPSHOT`, then `EV_WATERMARK` of the previous poll; an unknown
  value → `INVALID_WATERMARK`). `IT_OBJECTS` subscribes tables, optionally with range predicates evaluated on the
  current row image; deletes pass the table filter. Events (`ZSDTF_T_CDC_EVENT`) are table-row granular, carry a
  monotonically increasing `SEQ`, a `CHANGENR` grouping the rows of one business change, `OP` I/U/D, the UTC change
  timestamp and user, and the current row image (`JSON`; empty for D). Delivered in sequence order, `IV_PACKAGE` per
  call with `EV_EOF`, `EV_CHECKSUM` over the serialised events; events the filter drops still advance the watermark.
  Sequences are never re-issued: the platform keeps an idempotency ledger keyed by baseline run and `SEQ`.
* **No writes**: the function group contains read modules only.

## Reference sources (`src/`)
`z_sdtf_read_package.abap`, `z_sdtf_open_snapshot.abap`, `z_sdtf_table_metadata.abap`, `z_sdtf_cdc_poll.abap`, and `DDIC.md` for the types,
the `ZSDTF_SNAP` table and the helper classes. They are written for NW 7.50 (ECC 6.0 EHP8) syntax and have not been
activated anywhere.

## Platform side
* `SDTF_RFC_DEST_<SID>` (JSON `pyrfc.Connection` parameters; secrets as `env:NAME`) or `meta.rfc.dest` on the
  registered system; `SDTF_RFC_TRANSPORT=auto|pyrfc|simulated`, `SDTF_RFC_PACKAGE_SIZE`, `SDTF_RFC_KEY_CHUNK`,
  `SDTF_RFC_KEY_PUSHDOWN_LIMIT`.
* `POST /systems/{id}/connector/test` opens a snapshot, reads T001 metadata and one 5-row package.
* `python -m sdtf.cli demo --connector RFC` runs the whole vertical slice through the adapter on the simulated add-on.
* Delta: `POST /systems/{id}/simulate-changes` plays business activity into the simulated source's change log;
  `POST /runs/{baseline}/delta/cycles` captures, transforms, applies and reconciles it (`runtime/delta.py`, ADR-0014).
## Non-goals
* No write modules: loads go through released APIs, BAPIs or the Migration Cockpit on the target (ADR-0007).
* No direct database access that bypasses SAP semantics.

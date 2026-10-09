# DDIC objects of the add-on (reference, not transported)

| Object | Kind | Definition |
|---|---|---|
| `ZSDTF_S_PREDICATE` | structure | `FIELD TYPE FIELDNAME`, `OP TYPE CHAR2` (EQ NE BT NB GE GT LE LT CP NP), `LOW TYPE STRING`, `HIGH TYPE STRING` |
| `ZSDTF_T_PREDICATE` | table type | standard table of `ZSDTF_S_PREDICATE` |
| `ZSDTF_S_ROW` | structure | `ROWNO TYPE I`, `JSON TYPE STRING` (one row serialised by `/ui2/cl_json`, uppercase field names) |
| `ZSDTF_T_ROW` | table type | standard table of `ZSDTF_S_ROW` |
| `ZSDTF_S_FIELD` | structure | `FIELDNAME TYPE FIELDNAME`, `KEYFLAG TYPE KEYFLAG`, `DATATYPE TYPE DATATYPE_D`, `LENG TYPE DDLENG` |
| `ZSDTF_T_FIELD` | table type | standard table of `ZSDTF_S_FIELD` |
| `ZSDTF_S_WATERMARK` | structure | `TABNAME TYPE TABNAME`, `WATERMARK TYPE STRING` |
| `ZSDTF_T_WATERMARK` | table type | standard table of `ZSDTF_S_WATERMARK` |
| `ZSDTF_T_TABNAME` | table type | standard table of `TABNAME` |
| `ZSDTF_SNAP` | transparent table | `TOKEN TYPE CHAR40` (key), `CREATED_AT TYPE TIMESTAMP`, `CREATED_BY TYPE SYUNAME`, `VALID_UNTIL TYPE TIMESTAMP` |

Helper classes referenced by the function modules (to be implemented in the same package):

* `ZCL_SDTF_SNAPSHOT=>CHECK( iv_token )` — raises `UNKNOWN` / `EXPIRED` from `ZSDTF_SNAP`.
* `ZCL_SDTF_PREDICATE=>BUILD_WHERE( iv_table, it_predicate )` — validates every `FIELD` against the nametab,
  escapes values with `cl_abap_dyn_prg=>quote`, groups predicates per field (positives joined by `OR`, negatives by
  `AND NOT`, fields by `AND`), maps `CP`/`NP` to `LIKE` with `*`→`%`, `+`→`_`; returns the WHERE string and a hash of
  the normalised predicate list used to bind cursors.
* `ZCL_SDTF_PREDICATE=>KEY_FIELDS( iv_table )` — primary key fields without `MANDT`, in key order.
* `ZCL_SDTF_PREDICATE=>BUILD_KEYSET_AFTER( it_keyfields, it_lastkey )` — `(k1 > a) OR (k1 = a AND k2 > b) OR …`.
* `ZCL_SDTF_CURSOR=>ENCODE/DECODE` — base64 JSON `{t, p, k[], s}`; `DECODE` raises `INVALID` when the table,
  predicate hash or snapshot differ from the current call.

Authorizations for the technical RFC user: `S_RFC` (FUGR `ZSDTF`), `S_TABU_NAM` activity 03 for the tables in
scope only, no `S_TABU_DIS` wildcard. The add-on contains no write module.

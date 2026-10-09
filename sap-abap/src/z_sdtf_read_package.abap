FUNCTION z_sdtf_read_package.
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IT_PREDICATE) TYPE  ZSDTF_T_PREDICATE OPTIONAL
*"     VALUE(IV_PACKAGE) TYPE  I DEFAULT 5000
*"     VALUE(IV_CURSOR) TYPE  STRING OPTIONAL
*"     VALUE(IV_SNAPSHOT) TYPE  STRING
*"  EXPORTING
*"     VALUE(ET_ROWS) TYPE  ZSDTF_T_ROW
*"     VALUE(EV_CURSOR) TYPE  STRING
*"     VALUE(EV_EOF) TYPE  ABAP_BOOL
*"     VALUE(EV_CHECKSUM) TYPE  STRING
*"     VALUE(EV_ROWS) TYPE  I
*"  EXCEPTIONS
*"      NOT_AUTHORIZED
*"      TABLE_UNKNOWN
*"      SNAPSHOT_UNKNOWN
*"      SNAPSHOT_EXPIRED
*"      INVALID_CURSOR
*"      INVALID_PREDICATE
*"----------------------------------------------------------------------
* REFERENCE IMPLEMENTATION - NOT COMPILED, NOT TRANSPORTED, NOT TESTED ON AN SAP SYSTEM.
* It documents the behaviour the platform's RFC adapter (backend/sdtf/runtime/rfc.py) relies on and that the
* simulated add-on enforces in tests. An ABAP developer must review it against the target release (NW 7.50 for
* ECC 6.0 EHP8) before use. Only reads; no write modules exist in the add-on (ADR-0007).
*
* Behaviour:
*   1. S_TABU_NAM authorization (activity 03) for IV_TABLE; the technical user holds it only for tables in scope.
*   2. IV_SNAPSHOT must be a token issued by Z_SDTF_OPEN_SNAPSHOT and still valid (ZSDTF_SNAP).
*   3. IT_PREDICATE follows range-table semantics (same field: positives OR-ed, negatives AND-ed; fields AND-ed).
*   4. Rows are returned in primary-key order, IV_PACKAGE at a time (clamped to gc_max_package); the cursor is the
*      last primary key of the previous package (keyset pagination: works on 7.40+, no OFFSET needed), bound to
*      table, predicate hash and snapshot.
*   5. Each row is serialised as JSON (uppercase field names); EV_CHECKSUM = SHA-256 over the row JSON strings joined
*      with LF, so the client can verify transport integrity without re-serialising.

  CONSTANTS: gc_max_package TYPE i VALUE 10000.

  DATA: lv_where     TYPE string,
        lv_order     TYPE string,
        lv_phash     TYPE string,
        lt_keyfields TYPE STANDARD TABLE OF fieldname WITH EMPTY KEY,
        lt_lastkey   TYPE STANDARD TABLE OF string WITH EMPTY KEY,
        lr_data      TYPE REF TO data,
        lv_package   TYPE i,
        lv_concat    TYPE string,
        lv_json      TYPE string,
        ls_row       TYPE zsdtf_s_row,
        lv_rowno     TYPE i.
  FIELD-SYMBOLS: <lt_rows> TYPE STANDARD TABLE,
                 <ls_row>  TYPE any.

  CLEAR: et_rows, ev_cursor, ev_eof, ev_checksum, ev_rows.

* 1. authorization and table existence ---------------------------------------------------------------------
  AUTHORITY-CHECK OBJECT 'S_TABU_NAM' ID 'TABLE' FIELD iv_table ID 'ACTVT' FIELD '03'.
  IF sy-subrc <> 0.
    RAISE not_authorized.
  ENDIF.
  CALL FUNCTION 'DDIF_NAMETAB_GET'
    EXPORTING  tabname   = iv_table
    EXCEPTIONS not_found = 1 OTHERS = 2.
  IF sy-subrc <> 0.
    RAISE table_unknown.
  ENDIF.

* 2. snapshot token -----------------------------------------------------------------------------------------
  zcl_sdtf_snapshot=>check( EXPORTING iv_token = iv_snapshot
                            EXCEPTIONS unknown = 1 expired = 2 ).
  CASE sy-subrc.
    WHEN 1. RAISE snapshot_unknown.
    WHEN 2. RAISE snapshot_expired.
  ENDCASE.

* 3. predicate -> dynamic WHERE (every field name validated against the nametab, values escaped) ------------
  zcl_sdtf_predicate=>build_where( EXPORTING iv_table = iv_table it_predicate = it_predicate
                                   IMPORTING ev_where = lv_where ev_hash = lv_phash
                                   EXCEPTIONS invalid = 1 ).
  IF sy-subrc <> 0.
    RAISE invalid_predicate.
  ENDIF.

* 4. keyset cursor: WHERE (key > last) in primary-key order ------------------------------------------------
  zcl_sdtf_predicate=>key_fields( EXPORTING iv_table = iv_table IMPORTING et_fields = lt_keyfields ).
  IF iv_cursor IS NOT INITIAL.
    zcl_sdtf_cursor=>decode( EXPORTING iv_cursor = iv_cursor iv_table = iv_table iv_phash = lv_phash iv_snapshot = iv_snapshot
                             IMPORTING et_lastkey = lt_lastkey
                             EXCEPTIONS invalid = 1 ).
    IF sy-subrc <> 0.
      RAISE invalid_cursor.
    ENDIF.
    DATA(lv_after) = zcl_sdtf_predicate=>build_keyset_after( it_keyfields = lt_keyfields it_lastkey = lt_lastkey ).
    IF lv_where IS INITIAL.
      lv_where = lv_after.
    ELSE.
      lv_where = |( { lv_where } ) AND ( { lv_after } )|.
    ENDIF.
  ENDIF.
  lv_order = concat_lines_of( table = lt_keyfields sep = ` ` ).
  lv_package = COND #( WHEN iv_package < 1 THEN 1 WHEN iv_package > gc_max_package THEN gc_max_package ELSE iv_package ).

* 5. read one package more than requested to know whether more data exists --------------------------------
  CREATE DATA lr_data TYPE STANDARD TABLE OF (iv_table).
  ASSIGN lr_data->* TO <lt_rows>.
  SELECT * FROM (iv_table)
    WHERE (lv_where)
    ORDER BY (lv_order)
    INTO TABLE @<lt_rows>
    UP TO @( lv_package + 1 ) ROWS.
  IF lines( <lt_rows> ) > lv_package.
    DELETE <lt_rows> INDEX lv_package + 1.
    ev_eof = abap_false.
  ELSE.
    ev_eof = abap_true.
  ENDIF.

* 6. serialise, checksum, cursor ----------------------------------------------------------------------------
  LOOP AT <lt_rows> ASSIGNING <ls_row>.
    lv_rowno = lv_rowno + 1.
    lv_json = /ui2/cl_json=>serialize( data = <ls_row> compress = abap_false pretty_name = /ui2/cl_json=>pretty_mode-none ).
    ls_row-rowno = lv_rowno.
    ls_row-json  = lv_json.
    APPEND ls_row TO et_rows.
    IF lv_rowno > 1.
      lv_concat = lv_concat && cl_abap_char_utilities=>newline.
    ENDIF.
    lv_concat = lv_concat && lv_json.
  ENDLOOP.
  ev_rows = lv_rowno.
  cl_abap_message_digest=>calculate_hash_for_char(
    EXPORTING if_algorithm = 'SHA256' if_data = lv_concat
    IMPORTING ef_hashstring = ev_checksum ).
  TRANSLATE ev_checksum TO LOWER CASE.
  IF ev_eof = abap_false AND lv_rowno > 0.
    READ TABLE <lt_rows> ASSIGNING <ls_row> INDEX lv_rowno.
    ev_cursor = zcl_sdtf_cursor=>encode( iv_table = iv_table iv_phash = lv_phash iv_snapshot = iv_snapshot
                                         it_keyfields = lt_keyfields is_row = <ls_row> ).
  ENDIF.
ENDFUNCTION.

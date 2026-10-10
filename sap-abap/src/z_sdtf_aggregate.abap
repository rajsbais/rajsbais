FUNCTION z_sdtf_aggregate.
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IT_PREDICATE) TYPE  ZSDTF_T_PREDICATE OPTIONAL
*"     VALUE(IT_GROUP_BY) TYPE  ZSDTF_T_FIELDNAME OPTIONAL
*"     VALUE(IT_SUM) TYPE  ZSDTF_T_FIELDNAME OPTIONAL
*"     VALUE(IV_SNAPSHOT) TYPE  STRING
*"  EXPORTING
*"     VALUE(ET_ROWS) TYPE  ZSDTF_T_ROW
*"     VALUE(EV_CHECKSUM) TYPE  STRING
*"     VALUE(EV_ROWS) TYPE  I
*"  EXCEPTIONS
*"      NOT_AUTHORIZED
*"      TABLE_UNKNOWN
*"      SNAPSHOT_UNKNOWN
*"      SNAPSHOT_EXPIRED
*"      INVALID_PREDICATE
*"      INVALID_FIELD
*"----------------------------------------------------------------------
* REFERENCE IMPLEMENTATION - NOT COMPILED, NOT TRANSPORTED, NOT TESTED ON AN SAP SYSTEM.
* Totals computed in the source database for the reconciliation (backend/sdtf/reconciliation/views.py): one row
* per group with the group fields, COUNT and SUM_<FIELD> for every summed field. The platform uses it (a) to know
* how many rows a reconciliation read will transfer before reading them, and (b) to prove that the rows it read
* are complete (count and DMBTR totals per company code and debit/credit indicator). Read-only, like the rest of
* the add-on; the same authorization, snapshot and predicate rules as Z_SDTF_READ_PACKAGE.
*
* Behaviour:
*   1. S_TABU_NAM (activity 03) for IV_TABLE; unknown table -> TABLE_UNKNOWN.
*   2. IV_SNAPSHOT must be a valid token (the aggregate belongs to the same consistent read as the packages).
*   3. IT_PREDICATE -> WHERE as in Z_SDTF_READ_PACKAGE; every field in IT_GROUP_BY / IT_SUM is validated against the
*      nametab (INVALID_FIELD); summed fields must be numeric (CURR, QUAN, DEC, INT, FLTP).
*   4. SELECT <group fields>, COUNT( * ) AS count, SUM( f ) AS sum_f ... GROUP BY <group fields>; with no group
*      field one row with the totals is returned (COUNT 0 and sums 0 for an empty result).
*   5. Rows are serialised like packages (JSON, uppercase names: group fields, COUNT, SUM_<FIELD>); EV_CHECKSUM is
*      the SHA-256 over the row JSON strings joined with LF.

  DATA: lv_where   TYPE string,
        lv_phash   TYPE string,
        lv_select  TYPE string,
        lv_group   TYPE string,
        lr_data    TYPE REF TO data,
        lt_comp    TYPE cl_abap_structdescr=>component_table,
        lv_concat  TYPE string,
        lv_json    TYPE string,
        ls_row     TYPE zsdtf_s_row,
        lv_rowno   TYPE i.
  FIELD-SYMBOLS: <lt_rows> TYPE STANDARD TABLE,
                 <ls_row>  TYPE any.

  CLEAR: et_rows, ev_checksum, ev_rows.

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

* 3. predicate and field validation ------------------------------------------------------------------------
  zcl_sdtf_predicate=>build_where( EXPORTING iv_table = iv_table it_predicate = it_predicate
                                   IMPORTING ev_where = lv_where ev_hash = lv_phash
                                   EXCEPTIONS invalid = 1 ).
  IF sy-subrc <> 0.
    RAISE invalid_predicate.
  ENDIF.
  zcl_sdtf_predicate=>validate_fields( EXPORTING iv_table = iv_table it_group_by = it_group_by it_sum = it_sum
                                       IMPORTING et_components = lt_comp   " group fields, COUNT (INT8), SUM_<F> (same type as F)
                                                 ev_select = lv_select     " "BUKRS, SHKZG, COUNT( * ) AS count, SUM( dmbtr ) AS sum_dmbtr"
                                                 ev_group = lv_group       " "BUKRS, SHKZG"
                                       EXCEPTIONS invalid = 1 ).
  IF sy-subrc <> 0.
    RAISE invalid_field.
  ENDIF.

* 4. aggregate in the database ------------------------------------------------------------------------------
  CREATE DATA lr_data TYPE HANDLE cl_abap_tabledescr=>create( cl_abap_structdescr=>create( lt_comp ) ).
  ASSIGN lr_data->* TO <lt_rows>.
  IF lv_group IS INITIAL.
    SELECT (lv_select) FROM (iv_table) WHERE (lv_where) INTO TABLE @<lt_rows>.
    IF <lt_rows> IS INITIAL.
      APPEND INITIAL LINE TO <lt_rows>.
    ENDIF.
  ELSE.
    SELECT (lv_select) FROM (iv_table) WHERE (lv_where) GROUP BY (lv_group) ORDER BY (lv_group) INTO TABLE @<lt_rows>.
  ENDIF.

* 5. serialise and checksum ---------------------------------------------------------------------------------
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
ENDFUNCTION.

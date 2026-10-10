FUNCTION z_sdtf_table_metadata.
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"  EXPORTING
*"     VALUE(ET_FIELDS) TYPE  ZSDTF_T_FIELD
*"     VALUE(EV_ROWS) TYPE  I
*"     VALUE(EV_SIZE_MB) TYPE  P
*"     VALUE(EV_TABCLASS) TYPE  TABCLASS
*"     VALUE(EV_AUTHORIZED) TYPE  ABAP_BOOL
*"  EXCEPTIONS
*"      TABLE_UNKNOWN
*"----------------------------------------------------------------------
* REFERENCE IMPLEMENTATION - NOT COMPILED, NOT TRANSPORTED, NOT TESTED ON AN SAP SYSTEM.
*
* Read-only DDIC and size information for discovery. The row count comes from the database statistics
* (DB02-style, no COUNT(*) on large tables); EV_AUTHORIZED tells the platform whether the technical user may
* read the table, so discovery can list tables it will not be able to extract.

  DATA: lt_dfies TYPE STANDARD TABLE OF dfies,
        ls_field TYPE zsdtf_s_field,
        ls_dd02l TYPE dd02l.

  CLEAR: et_fields, ev_rows, ev_size_mb, ev_tabclass, ev_authorized.

  SELECT SINGLE * FROM dd02l INTO ls_dd02l WHERE tabname = iv_table AND as4local = 'A'.
  IF sy-subrc <> 0.
    RAISE table_unknown.
  ENDIF.
  ev_tabclass = ls_dd02l-tabclass.

  CALL FUNCTION 'DDIF_FIELDINFO_GET'
    EXPORTING  tabname   = iv_table
    TABLES     dfies_tab = lt_dfies
    EXCEPTIONS not_found = 1 OTHERS = 2.
  IF sy-subrc <> 0.
    RAISE table_unknown.
  ENDIF.
  LOOP AT lt_dfies INTO DATA(ls_dfies) WHERE fieldname <> 'MANDT'.
    CLEAR ls_field.
    ls_field-fieldname = ls_dfies-fieldname.
    ls_field-keyflag   = ls_dfies-keyflag.
    ls_field-datatype  = ls_dfies-datatype.
    ls_field-leng      = ls_dfies-leng.
    APPEND ls_field TO et_fields.
  ENDLOOP.

* size and row estimate from database statistics (release/DB specific; DB_STATISTICS_DATA_READ is one option)
  CALL FUNCTION 'DB_GET_TABLE_SIZE'
    EXPORTING  tabname   = iv_table
    IMPORTING  size_mb   = ev_size_mb
               row_count = ev_rows
    EXCEPTIONS OTHERS    = 1.
  IF sy-subrc <> 0.
    CLEAR: ev_size_mb, ev_rows.
  ENDIF.

  AUTHORITY-CHECK OBJECT 'S_TABU_NAM' ID 'TABLE' FIELD iv_table ID 'ACTVT' FIELD '03'.
  ev_authorized = boolc( sy-subrc = 0 ).
ENDFUNCTION.

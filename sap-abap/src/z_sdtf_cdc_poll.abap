FUNCTION z_sdtf_cdc_poll.
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IV_WATERMARK) TYPE  STRING
*"     VALUE(IT_OBJECTS) TYPE  ZSDTF_T_CDC_OBJECT
*"     VALUE(IV_PACKAGE) TYPE  I DEFAULT 1000
*"  EXPORTING
*"     VALUE(ET_EVENTS) TYPE  ZSDTF_T_CDC_EVENT
*"     VALUE(EV_WATERMARK) TYPE  STRING
*"     VALUE(EV_EOF) TYPE  ABAP_BOOL
*"     VALUE(EV_CHECKSUM) TYPE  STRING
*"     VALUE(EV_EVENTS) TYPE  I
*"  EXCEPTIONS
*"      NOT_AUTHORIZED
*"      INVALID_WATERMARK
*"----------------------------------------------------------------------
* REFERENCE IMPLEMENTATION - NOT COMPILED, NOT TRANSPORTED, NOT TESTED ON AN SAP SYSTEM.
*
* Change events after a watermark, at table-row granularity, grouped by CHANGENR so that the platform can
* replay a business change (document header + items) atomically. Sources of change information, by table:
*   * masters covered by change documents (KNA1/KNB1, LFA1/LFB1, MARA/MARC/...): CDHDR/CDPOS, CHANGENR = CDHDR-CHANGENR
*   * documents with creation/change timestamps (VBAK ERDAT/ERZET + AEDAT, BKPF CPUDT/CPUTM, EKKO AEDAT, LIKP ...):
*     timestamp watermark per table; items of a changed header are re-read with the header and share its CHANGENR
*   * deletes: table-logging (DBTABLOG) or change-document deletions; a delete carries no row image
* The watermark is an opaque string the add-on issues: the reference encodes (UTC timestamp, last CDHDR CHANGENR)
* in ZSDTF_CDC_WM so that polls are exactly-once per sequence; the platform treats it as opaque.
*
* Row images are the CURRENT state of the row (after all changes up to the poll), serialised like
* Z_SDTF_READ_PACKAGE (/ui2/cl_json, uppercase field names); EV_CHECKSUM is SHA-256 over the serialised events
* joined by LF. Predicates in IT_OBJECTS follow Z_SDTF_READ_PACKAGE semantics and apply to the row image;
* deletes pass the table filter unconditionally. The simulated add-on (backend/sdtf/runtime/rfc.py) is the
* executable reference for all of this.

  DATA: ls_wm      TYPE zsdtf_s_cdc_wm,
        lt_tables  TYPE SORTED TABLE OF tabname WITH UNIQUE KEY table_line,
        lv_seq     TYPE i,
        lv_concat  TYPE string,
        lv_json    TYPE string,
        ls_event   TYPE zsdtf_s_cdc_event.

  CLEAR: et_events, ev_watermark, ev_eof, ev_checksum, ev_events.

* watermark ---------------------------------------------------------------------------------------------------
  zcl_sdtf_cdc=>decode_watermark( EXPORTING iv_watermark = iv_watermark IMPORTING es_wm = ls_wm EXCEPTIONS invalid = 1 ).
  IF sy-subrc <> 0.
    RAISE invalid_watermark.
  ENDIF.

* tables and authorization ------------------------------------------------------------------------------------
  LOOP AT it_objects INTO DATA(ls_obj).
    AUTHORITY-CHECK OBJECT 'S_TABU_NAM' ID 'TABLE' FIELD ls_obj-tabname ID 'ACTVT' FIELD '03'.
    IF sy-subrc <> 0.
      RAISE not_authorized.
    ENDIF.
    INSERT ls_obj-tabname INTO TABLE lt_tables.
  ENDLOOP.

* 1. change documents (masters): CDHDR after the watermark, CDPOS rows for the subscribed tables -------------
  SELECT h~changenr, h~udate, h~utime, h~username, p~tabname, p~tabkey, p~chngind
    FROM cdhdr AS h INNER JOIN cdpos AS p ON p~objectclas = h~objectclas AND p~objectid = h~objectid AND p~changenr = h~changenr
    WHERE ( h~udate > @ls_wm-udate OR ( h~udate = @ls_wm-udate AND h~utime > @ls_wm-utime ) OR ( h~udate = @ls_wm-udate AND h~utime = @ls_wm-utime AND h~changenr > @ls_wm-changenr ) )
      AND p~tabname IN @lt_tables
    ORDER BY h~udate, h~utime, h~changenr, p~tabname, p~tabkey
    INTO TABLE @DATA(lt_cd)
    UP TO @( iv_package + 1 ) ROWS.

* 2. timestamp-watermarked documents: header tables with change timestamps, items re-read with the header ----
*    (per table: SELECT keys WHERE aedat/erdat >= wm date ... ORDER BY timestamp; items of each header by key)
  zcl_sdtf_cdc=>collect_document_changes( EXPORTING it_objects = it_objects is_wm = ls_wm iv_limit = iv_package + 1 - lines( lt_cd )
                                          CHANGING ct_cd = lt_cd ).

* 3. package, images, checksum --------------------------------------------------------------------------------
  IF lines( lt_cd ) > iv_package.
    DELETE lt_cd FROM iv_package + 1.
    ev_eof = abap_false.
  ELSE.
    ev_eof = abap_true.
  ENDIF.
  LOOP AT lt_cd INTO DATA(ls_cd).
    lv_seq = lv_seq + 1.
    CLEAR ls_event.
    ls_event-seq         = zcl_sdtf_cdc=>sequence_of( ls_cd ).       " monotonically increasing per source
    ls_event-changenr    = ls_cd-changenr.
    ls_event-object_type = zcl_sdtf_cdc=>object_type_of( ls_cd-tabname ).
    ls_event-tabname     = ls_cd-tabname.
    ls_event-key         = zcl_sdtf_cdc=>key_string_of( iv_table = ls_cd-tabname iv_tabkey = ls_cd-tabkey ).
    ls_event-op          = SWITCH #( ls_cd-chngind WHEN 'I' THEN 'I' WHEN 'D' THEN 'D' ELSE 'U' ).
    ls_event-changed_at  = |{ ls_cd-udate }{ ls_cd-utime }|.
    ls_event-changed_by  = ls_cd-username.
    IF ls_event-op <> 'D'.
      ls_event-json = zcl_sdtf_cdc=>row_image_json( iv_table = ls_cd-tabname iv_tabkey = ls_cd-tabkey ).  " current image, predicates applied
      IF ls_event-json IS INITIAL.
        CONTINUE.                                                     " row no longer matches the predicates / vanished
      ENDIF.
    ENDIF.
    APPEND ls_event TO et_events.
    lv_json = /ui2/cl_json=>serialize( data = ls_event compress = abap_false pretty_name = /ui2/cl_json=>pretty_mode-none ).
    IF lines( et_events ) > 1.
      lv_concat = lv_concat && cl_abap_char_utilities=>newline.
    ENDIF.
    lv_concat = lv_concat && lv_json.
    ls_wm-udate = ls_cd-udate. ls_wm-utime = ls_cd-utime. ls_wm-changenr = ls_cd-changenr.
  ENDLOOP.
  ev_events = lines( et_events ).
  cl_abap_message_digest=>calculate_hash_for_char( EXPORTING if_algorithm = 'SHA256' if_data = lv_concat IMPORTING ef_hashstring = ev_checksum ).
  TRANSLATE ev_checksum TO LOWER CASE.
  ev_watermark = zcl_sdtf_cdc=>encode_watermark( ls_wm ).
ENDFUNCTION.

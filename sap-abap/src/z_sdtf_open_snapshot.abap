FUNCTION z_sdtf_open_snapshot.
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IT_TABLES) TYPE  ZSDTF_T_TABNAME OPTIONAL
*"     VALUE(IV_TTL_SECONDS) TYPE  I DEFAULT 3600
*"  EXPORTING
*"     VALUE(EV_SNAPSHOT) TYPE  STRING
*"     VALUE(EV_VALID_UNTIL) TYPE  TIMESTAMP
*"     VALUE(ET_WATERMARKS) TYPE  ZSDTF_T_WATERMARK
*"----------------------------------------------------------------------
* REFERENCE IMPLEMENTATION - NOT COMPILED, NOT TRANSPORTED, NOT TESTED ON AN SAP SYSTEM.
*
* Issues the consistency token every package read of a run must carry. The token is a GUID persisted in
* ZSDTF_SNAP with its validity and the calling user; Z_SDTF_READ_PACKAGE rejects reads with an unknown or
* expired token (SNAPSHOT_UNKNOWN / SNAPSHOT_EXPIRED) so that a run cannot silently mix data from different
* points in time after a restart. Per-table watermarks record the last change timestamp known at open time;
* the delta engine (docs/07, planned) replays changes after the watermark.
*
* Where the database offers a stable read view (HANA: snapshot isolation per transaction; Oracle: flashback
* query) the token can be bound to it; this reference records watermarks only, which is what the platform's
* reconciliation needs to explain post-snapshot changes.

  DATA: ls_snap TYPE zsdtf_snap,
        ls_wm   TYPE zsdtf_s_watermark,
        lv_ts   TYPE timestamp.

  CLEAR: ev_snapshot, ev_valid_until, et_watermarks.

  GET TIME STAMP FIELD lv_ts.
  ls_snap-token       = |snap-{ cl_system_uuid=>create_uuid_c32_static( ) }|.
  ls_snap-created_at  = lv_ts.
  ls_snap-created_by  = sy-uname.
  ls_snap-valid_until = cl_abap_tstmp=>add( tstmp = lv_ts secs = COND #( WHEN iv_ttl_seconds < 60 THEN 60 ELSE iv_ttl_seconds ) ).
  INSERT zsdtf_snap FROM ls_snap.
  COMMIT WORK.

  LOOP AT it_tables INTO DATA(lv_table).
    CLEAR ls_wm.
    ls_wm-tabname = lv_table.
*   change-pointer / CDHDR based watermark where the table is covered by change documents; otherwise the
*   snapshot timestamp (the platform treats it as "no finer watermark available")
    SELECT MAX( udate ) AS udate, MAX( utime ) AS utime
      FROM cdhdr
      WHERE tcode <> @space
      INTO @DATA(ls_cd).
    IF sy-subrc = 0 AND ls_cd-udate IS NOT INITIAL.
      ls_wm-watermark = |{ ls_cd-udate }{ ls_cd-utime }|.
    ELSE.
      ls_wm-watermark = |{ lv_ts }|.
    ENDIF.
    APPEND ls_wm TO et_watermarks.
  ENDLOOP.

  ev_snapshot    = ls_snap-token.
  ev_valid_until = ls_snap-valid_until.
ENDFUNCTION.

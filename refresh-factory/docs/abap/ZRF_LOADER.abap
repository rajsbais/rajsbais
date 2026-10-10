*&---------------------------------------------------------------------*
*& ZRF loader, protocol RFL-1                                           *
*&                                                                     *
*& STATUS: WRITTEN, NEVER COMPILED OR RUN. Install it ONLY in a sandbox *
*& (for example the NPL ABAP trial), read it first, test every function *
*& module in SE37, and expect to fix syntax errors. It is the contract  *
*& the platform's RfcTargetAdapter and its fake (fake_loader.py) follow.*
*&                                                                     *
*& Safeguards this code must keep (the platform assumes all of them):    *
*&  1. refuse in a productive client (T000-CCCATEGORY = 'P')             *
*&  2. refuse unless the system owner switched it on (ZRF_CFG-ENABLED)   *
*&  3. refuse tables that are not on the owner's allow list (ZRF_ALLOW)  *
*&  4. check S_TABU_NAM activity 02 for the connected user and table     *
*&  5. at most 500 rows per call, all or nothing (ROLLBACK on any error) *
*&  6. a dry run that validates exactly like the real call and changes   *
*&     nothing                                                           *
*&  7. every call is logged (ZRF_LOG), also when it failed or was dry    *
*&                                                                     *
*& DDIC objects to create first (SE11), all client-dependent:            *
*&   ZRF_CFG   (MANDT key, ENABLED CHAR1)                                *
*&   ZRF_ALLOW (MANDT key, TABNAME TABNAME key)                          *
*&   ZRF_LOG   (MANDT key, REQID CHAR32 key, SEQNO NUMC4 key, TABNAME    *
*&              TABNAME, OP CHAR10, ROWS INT4, DRYRUN CHAR1, UNAME       *
*&              SYUNAME, UDATE DATS, UTIME TIMS, MSG CHAR120)            *
*&   ZRF_MSG   structure (MESSAGE CHAR200)                               *
*&   ZRF_T_TABNAME table type of TABNAME                                 *
*&   ZRF_T_MSG table type of ZRF_MSG                                     *
*& Function group ZRF with the four RFC-enabled function modules below.  *
*&---------------------------------------------------------------------*

*------- global data / forms of function group ZRF (LZRFTOP / LZRFF01) --
FORM guard USING iv_table TYPE tabname CHANGING cv_msg TYPE string.
  DATA: lv_cat TYPE t000-cccategory,
        lv_en  TYPE zrf_cfg-enabled,
        lv_cnt TYPE i.
  CLEAR cv_msg.
  SELECT SINGLE cccategory FROM t000 INTO lv_cat WHERE mandt = sy-mandt.
  IF lv_cat = 'P'.
    cv_msg = 'productive client'. RETURN.
  ENDIF.
  SELECT SINGLE enabled FROM zrf_cfg INTO lv_en WHERE mandt = sy-mandt.
  IF lv_en <> 'X'.
    cv_msg = 'writes are not enabled (ZRF_CFG)'. RETURN.
  ENDIF.
  SELECT COUNT(*) FROM zrf_allow INTO lv_cnt WHERE mandt = sy-mandt AND tabname = iv_table.
  IF lv_cnt = 0.
    cv_msg = |table { iv_table } is not on the allow list (ZRF_ALLOW)|. RETURN.
  ENDIF.
  AUTHORITY-CHECK OBJECT 'S_TABU_NAM' ID 'ACTVT' FIELD '02' ID 'TABLE' FIELD iv_table.
  IF sy-subrc <> 0.
    cv_msg = |no authorization S_TABU_NAM 02 for { iv_table }|.
  ENDIF.
ENDFORM.

FORM write_log USING iv_reqid TYPE char32 iv_table TYPE tabname iv_op TYPE char10 iv_rows TYPE i iv_dry TYPE char1 iv_msg TYPE string.
  DATA ls_log TYPE zrf_log.
  ls_log-mandt = sy-mandt. ls_log-reqid = iv_reqid.
  SELECT COUNT(*) FROM zrf_log INTO ls_log-seqno WHERE mandt = sy-mandt AND reqid = iv_reqid.
  ls_log-seqno = ls_log-seqno + 1.
  ls_log-tabname = iv_table. ls_log-op = iv_op. ls_log-rows = iv_rows. ls_log-dryrun = iv_dry.
  ls_log-uname = sy-uname. ls_log-udate = sy-datum. ls_log-utime = sy-uzeit. ls_log-msg = iv_msg.
  INSERT zrf_log FROM ls_log.
  COMMIT WORK.
ENDFORM.

*------- FM ZRF_PING ---------------------------------------------------
FUNCTION zrf_ping.
*"  EXPORTING
*"     VALUE(EV_VERSION) TYPE  CHAR10
*"     VALUE(EV_SYSID) TYPE  SYSYSID
*"     VALUE(EV_CLIENT) TYPE  MANDT
*"     VALUE(EV_CLIENT_CATEGORY) TYPE  CHAR1
*"     VALUE(EV_WRITES_ENABLED) TYPE  CHAR1
*"     VALUE(ET_ALLOWED) TYPE  ZRF_T_TABNAME
  DATA lv_en TYPE zrf_cfg-enabled.
  ev_version = 'RFL-1'.
  ev_sysid   = sy-sysid.
  ev_client  = sy-mandt.
  SELECT SINGLE cccategory FROM t000 INTO ev_client_category WHERE mandt = sy-mandt.
  SELECT SINGLE enabled FROM zrf_cfg INTO lv_en WHERE mandt = sy-mandt.
  IF lv_en = 'X' AND ev_client_category <> 'P'.
    ev_writes_enabled = 'X'.
  ENDIF.
  SELECT tabname FROM zrf_allow INTO TABLE et_allowed WHERE mandt = sy-mandt.
ENDFUNCTION.

*------- FM ZRF_UPSERT -------------------------------------------------
FUNCTION zrf_upsert.
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_ROWS) TYPE  STRING
*"     VALUE(IV_DRY_RUN) TYPE  CHAR1 OPTIONAL
*"     VALUE(IV_REQUEST_ID) TYPE  CHAR32
*"  EXPORTING
*"     VALUE(EV_WRITTEN) TYPE  I
*"     VALUE(ET_MESSAGES) TYPE  ZRF_T_MSG
  DATA: lr_tab TYPE REF TO data,
        lv_msg TYPE string,
        ls_msg TYPE zrf_msg,
        lv_n   TYPE i.
  FIELD-SYMBOLS: <tab> TYPE STANDARD TABLE, <row> TYPE any, <f> TYPE any.
  ev_written = 0.
  PERFORM guard USING iv_table CHANGING lv_msg.
  IF lv_msg IS INITIAL.
    TRY.
        CREATE DATA lr_tab TYPE STANDARD TABLE OF (iv_table) WITH DEFAULT KEY.
        ASSIGN lr_tab->* TO <tab>.
        /ui2/cl_json=>deserialize( EXPORTING json = iv_rows pretty_name = /ui2/cl_json=>pretty_mode-none CHANGING data = <tab> ).
      CATCH cx_root INTO DATA(lx).
        lv_msg = |rows could not be read: { lx->get_text( ) }|.
    ENDTRY.
  ENDIF.
  IF lv_msg IS INITIAL.
    lv_n = lines( <tab> ).
    IF lv_n = 0 OR lv_n > 500.
      lv_msg = |{ lv_n } rows: between 1 and 500 are accepted per call|.
    ENDIF.
  ENDIF.
  IF lv_msg IS INITIAL.
    LOOP AT <tab> ASSIGNING <row>.        "client field, if the table has one
      ASSIGN COMPONENT 'MANDT' OF STRUCTURE <row> TO <f>.
      IF sy-subrc = 0. <f> = sy-mandt. ENDIF.
    ENDLOOP.
    IF iv_dry_run = 'X'.
      ev_written = lv_n.
    ELSE.
      MODIFY (iv_table) FROM TABLE <tab>.
      IF sy-subrc = 0 AND sy-dbcnt = lv_n.
        COMMIT WORK.
        ev_written = lv_n.
      ELSE.
        ROLLBACK WORK.
        lv_msg = |rolled back: { sy-dbcnt } of { lv_n } rows modified|.
      ENDIF.
    ENDIF.
  ENDIF.
  IF lv_msg IS NOT INITIAL.
    ls_msg-message = lv_msg. APPEND ls_msg TO et_messages.
  ENDIF.
  PERFORM write_log USING iv_request_id iv_table 'UPSERT' ev_written iv_dry_run lv_msg.
ENDFUNCTION.

*------- FM ZRF_DELETE: same shape; IV_KEYS holds rows with the KEY fields only ---
FUNCTION zrf_delete.
*"  IMPORTING
*"     VALUE(IV_TABLE) TYPE  TABNAME
*"     VALUE(IV_KEYS) TYPE  STRING
*"     VALUE(IV_DRY_RUN) TYPE  CHAR1 OPTIONAL
*"     VALUE(IV_REQUEST_ID) TYPE  CHAR32
*"  EXPORTING
*"     VALUE(EV_WRITTEN) TYPE  I
*"     VALUE(ET_MESSAGES) TYPE  ZRF_T_MSG
  DATA: lr_tab TYPE REF TO data, lv_msg TYPE string, ls_msg TYPE zrf_msg, lv_n TYPE i.
  FIELD-SYMBOLS: <tab> TYPE STANDARD TABLE, <row> TYPE any, <f> TYPE any.
  ev_written = 0.
  PERFORM guard USING iv_table CHANGING lv_msg.
  IF lv_msg IS INITIAL.
    TRY.
        CREATE DATA lr_tab TYPE STANDARD TABLE OF (iv_table) WITH DEFAULT KEY.
        ASSIGN lr_tab->* TO <tab>.
        /ui2/cl_json=>deserialize( EXPORTING json = iv_keys pretty_name = /ui2/cl_json=>pretty_mode-none CHANGING data = <tab> ).
      CATCH cx_root INTO DATA(lx).
        lv_msg = |keys could not be read: { lx->get_text( ) }|.
    ENDTRY.
  ENDIF.
  IF lv_msg IS INITIAL.
    lv_n = lines( <tab> ).
    IF lv_n = 0 OR lv_n > 500.
      lv_msg = |{ lv_n } keys: between 1 and 500 are accepted per call|.
    ENDIF.
  ENDIF.
  IF lv_msg IS INITIAL.
    LOOP AT <tab> ASSIGNING <row>.
      ASSIGN COMPONENT 'MANDT' OF STRUCTURE <row> TO <f>.
      IF sy-subrc = 0. <f> = sy-mandt. ENDIF.
    ENDLOOP.
    IF iv_dry_run = 'X'.
      ev_written = lv_n.
    ELSE.
      DELETE (iv_table) FROM TABLE <tab>.
      IF sy-subrc = 0 AND sy-dbcnt = lv_n.
        COMMIT WORK.
        ev_written = lv_n.
      ELSE.
        ROLLBACK WORK.
        lv_msg = |rolled back: { sy-dbcnt } of { lv_n } rows deleted|.
      ENDIF.
    ENDIF.
  ENDIF.
  IF lv_msg IS NOT INITIAL.
    ls_msg-message = lv_msg. APPEND ls_msg TO et_messages.
  ENDIF.
  PERFORM write_log USING iv_request_id iv_table 'DELETE' ev_written iv_dry_run lv_msg.
ENDFUNCTION.

*------- FM ZRF_NR_SET: raises a number range level (NRIV). UNVERIFIED APPROACH: ---
*------- it updates NRIV directly; buffered ranges need a buffer reset (SM56) and  ---
*------- the supported API (NUMBER_RANGE_INTERVAL_UPDATE) should replace this.    ---
FUNCTION zrf_nr_set.
*"  IMPORTING
*"     VALUE(IV_OBJECT) TYPE  NRIV-OBJECT
*"     VALUE(IV_LEVEL) TYPE  STRING
*"     VALUE(IV_DRY_RUN) TYPE  CHAR1 OPTIONAL
*"     VALUE(IV_REQUEST_ID) TYPE  CHAR32
*"  EXPORTING
*"     VALUE(EV_WRITTEN) TYPE  I
*"     VALUE(ET_MESSAGES) TYPE  ZRF_T_MSG
  DATA: lv_msg TYPE string, ls_msg TYPE zrf_msg, ls_nriv TYPE nriv, lv_lvl TYPE nriv-nrlevel.
  ev_written = 0.
  PERFORM guard USING 'NRIV' CHANGING lv_msg.
  IF lv_msg IS INITIAL.
    SELECT SINGLE * FROM nriv INTO ls_nriv WHERE client = sy-mandt AND object = iv_object AND nrrangenr = '01'.
    IF sy-subrc <> 0.
      lv_msg = |number range object { iv_object } / 01 not found|.
    ENDIF.
  ENDIF.
  IF lv_msg IS INITIAL.
    lv_lvl = |{ iv_level ALPHA = IN WIDTH = strlen( ls_nriv-fromnumber ) }|.
    IF lv_lvl < ls_nriv-nrlevel.
      lv_msg = 'the level would go down: refused'.
    ELSEIF lv_lvl > ls_nriv-tonumber.
      lv_msg = 'the level is beyond the interval: refused'.
    ELSEIF iv_dry_run = 'X'.
      ev_written = 1.
    ELSE.
      UPDATE nriv SET nrlevel = lv_lvl WHERE client = sy-mandt AND object = iv_object AND nrrangenr = '01'.
      IF sy-subrc = 0. COMMIT WORK. ev_written = 1. ELSE. ROLLBACK WORK. lv_msg = 'update failed'. ENDIF.
    ENDIF.
  ENDIF.
  IF lv_msg IS NOT INITIAL.
    ls_msg-message = lv_msg. APPEND ls_msg TO et_messages.
  ENDIF.
  PERFORM write_log USING iv_request_id 'NRIV' 'NR_SET' ev_written iv_dry_run lv_msg.
ENDFUNCTION.

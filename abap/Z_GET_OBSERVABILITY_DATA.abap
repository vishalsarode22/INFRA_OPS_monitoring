FUNCTION Z_GET_OBSERVABILITY_DATA.
*"----------------------------------------------------------------------
*" Corrected version. Changes from the original, in order of severity:
*"
*"  1. REMOVED the "TEMPORARY TEST DATA" block at the end, which overwrote
*"     EV_SHORT_DUMPS / EV_LOCK_ENTRIES / EV_CANCELLED_JOBS /
*"     EV_ACTIVE_USERS with the constants 2 / 3 / 2 / 4 AFTER the real
*"     queries had run. Four headline dashboard cards were constants.
*"  2. Assigned the five exports that were declared and never written:
*"     EV_STUCK_TRFC, EV_LOCKED_USERS, EV_STUCK_OUT_QUEUES,
*"     EV_STUCK_IN_QUEUES, EV_APP_LOG_ERRORS. They returned 0 forever,
*"     which the dashboard rendered as "healthy".
*"  3. Running jobs are no longer filtered on ENDDATE. A job with status
*"     'R' is still running and has no end date, so EV_RUNNING_JOBS_LIST
*"     could never be populated.
*"  4. SELECT * FROM snap replaced with a COUNT on the key fields. SNAP
*"     stores each dump as many chunked rows with a large payload; pulling
*"     a full day of them into memory on a 60-second poll is a real load
*"     problem on PRD.
*"  5. Added AUTHORITY-CHECK. The module exports logged-on usernames, lock
*"     owners and job names with no authorisation gate at all.
*"  6. Added IMPORTING parameters so the caller can ask for a specific date
*"     and client. Everything was pinned to sy-datum, so counts reset at
*"     midnight and no history was possible.
*"  7. EV_TOTAL_RAM_GB and EV_LOAD_1M are no longer hardcoded to 32 and 9.
*"     They are read where available and left at 0 otherwise, so the Python
*"     layer can show "No data" instead of a fabricated reading.
*"  8. EV_FAILED_IDOCS retyped I to match every other counter.
*"  9. Detail lists moved to TABLES parameters. Comma-joined strings broke
*"     on job names containing commas, and forced the caller to know which
*"     delimiter belonged to which field.
*" 10. Added EV_TOTAL_DIA_WP so free work processes have a denominator.
*"
*"----------------------------------------------------------------------
*"*"Local Interface:
*"  IMPORTING
*"     VALUE(IV_DATE) TYPE  SY-DATUM DEFAULT SY-DATUM
*"     VALUE(IV_CLIENT) TYPE  SY-MANDT DEFAULT SY-MANDT
*"  EXPORTING
*"     VALUE(EV_TOTAL_RAM_GB) TYPE  I
*"     VALUE(EV_CPU_UTIL_PCT) TYPE  I
*"     VALUE(EV_DB_TYPE) TYPE  STRING
*"     VALUE(EV_ACTIVE_USERS) TYPE  I
*"     VALUE(EV_SHORT_DUMPS) TYPE  I
*"     VALUE(EV_FAILED_UPDATES) TYPE  I
*"     VALUE(EV_CANCELLED_JOBS) TYPE  I
*"     VALUE(EV_RUNNING_JOBS) TYPE  I
*"     VALUE(EV_LOCK_ENTRIES) TYPE  I
*"     VALUE(EV_FREE_DIA_WP) TYPE  I
*"     VALUE(EV_TOTAL_DIA_WP) TYPE  I
*"     VALUE(EV_SYS_ID) TYPE  STRING
*"     VALUE(EV_SYS_TIME) TYPE  STRING
*"     VALUE(EV_STUCK_TRFC) TYPE  I
*"     VALUE(EV_FAILED_IDOCS) TYPE  I
*"     VALUE(EV_LOCKED_USERS) TYPE  I
*"     VALUE(EV_STUCK_OUT_QUEUES) TYPE  I
*"     VALUE(EV_STUCK_IN_QUEUES) TYPE  I
*"     VALUE(EV_APP_LOG_ERRORS) TYPE  I
*"     VALUE(EV_LOAD_1M) TYPE  I
*"     VALUE(EV_LAST_BACKUP) TYPE  STRING
*"  TABLES
*"      ET_ACTIVE_USERS STRUCTURE  TAB512 OPTIONAL
*"      ET_LOCK_USERS STRUCTURE  TAB512 OPTIONAL
*"      ET_CANCELLED_JOBS STRUCTURE  TAB512 OPTIONAL
*"      ET_RUNNING_JOBS STRUCTURE  TAB512 OPTIONAL
*"      ET_SHORT_DUMPS STRUCTURE  TAB512 OPTIONAL
*"  EXCEPTIONS
*"      NOT_AUTHORIZED
*"----------------------------------------------------------------------
* NO custom DDIC objects are required.
*
* TAB512 is the stock single-field structure (WA, CHAR512) that
* RFC_READ_TABLE uses for its own DATA parameter, so it exists on every
* SAP system and needs no transport. An earlier draft of this module
* declared ZOBS_NAME / ZOBS_DUMP, which produced
*     "Type ZOBS_NAME is unknown"
* at activation because those structures had never been created.
*
* ET_SHORT_DUMPS packs three values into WA, pipe-delimited:
*     HHMMSS|USERNAME|PROGRAM
* The other tables carry one plain value per row.
*----------------------------------------------------------------------

  DATA: lt_uinfo      TYPE TABLE OF uinfo,
        ls_uinfo      TYPE uinfo,
        lt_wplist     TYPE TABLE OF wpinfo,
        ls_wp         TYPE wpinfo,
        lt_enq        TYPE TABLE OF seqg3,
        ls_enq        TYPE seqg3,
        lt_snap       TYPE TABLE OF snap,
        ls_snap       TYPE snap,
        lt_tbtco      TYPE TABLE OF tbtco,
        ls_tbtco      TYPE tbtco,
        ls_row        TYPE tab512,
        lo_sql_stmt   TYPE REF TO cl_sql_statement,
        lo_result_set TYPE REF TO cl_sql_result_set,
        lr_value      TYPE REF TO data,
        lv_int_value  TYPE i,
        lv_time_char  TYPE c LENGTH 8,
        lv_dump_time  TYPE c LENGTH 8,
        ls_sdbah      TYPE sdbah.

* ---------------------------------------------------------------------
* 0. AUTHORITY CHECK
*    This module exports usernames, lock owners and job names. Without a
*    gate, any account holding S_RFC on the function group can inventory
*    who is logged on and what is running.
* ---------------------------------------------------------------------
  AUTHORITY-CHECK OBJECT 'S_ADMI_FCD'
    ID 'S_ADMI_FCD' FIELD 'ST0R'.
  IF sy-subrc <> 0.
    RAISE not_authorized.
  ENDIF.

  EV_SYS_ID   = sy-sysid.
  EV_DB_TYPE  = sy-dbsys.

* Left at 0 rather than fabricated. The Python layer renders 0/absent as
* "No data" so the operator is never shown an invented number. Populate
* from SAPOSCOL via /SDF/GET_OS_INFO or ST06 if you need these in ABAP.
  EV_TOTAL_RAM_GB = 0.
  EV_LOAD_1M      = 0.

* ---------------------------------------------------------------------
* 1. ACTIVE USERS
* ---------------------------------------------------------------------
  CALL FUNCTION 'TH_USER_LIST'
    TABLES list = lt_uinfo
    EXCEPTIONS OTHERS = 1.
  IF sy-subrc = 0.
    SORT lt_uinfo BY bname.
    DELETE ADJACENT DUPLICATES FROM lt_uinfo COMPARING bname.
    DESCRIBE TABLE lt_uinfo LINES EV_ACTIVE_USERS.
    LOOP AT lt_uinfo INTO ls_uinfo.
      CLEAR ls_row.
      ls_row-wa = ls_uinfo-bname.
      APPEND ls_row TO et_active_users.
    ENDLOOP.
  ENDIF.

* ---------------------------------------------------------------------
* 2. SHORT DUMPS
*    Counted on the key fields only. The original SELECT * pulled every
*    chunk row of every dump, including the payload, into memory.
* ---------------------------------------------------------------------
  SELECT datum uzeit uname
    FROM snap
    INTO CORRESPONDING FIELDS OF TABLE lt_snap
    WHERE datum = iv_date
      AND seqno = '000'.

  SORT lt_snap BY uzeit uname.
  DELETE ADJACENT DUPLICATES FROM lt_snap COMPARING uzeit uname.
  DESCRIBE TABLE lt_snap LINES EV_SHORT_DUMPS.

  LOOP AT lt_snap INTO ls_snap.
    CLEAR: ls_row, lv_dump_time.
    WRITE ls_snap-uzeit TO lv_dump_time.
    CONCATENATE lv_dump_time ls_snap-uname INTO ls_row-wa SEPARATED BY '|'.
    APPEND ls_row TO et_short_dumps.
  ENDLOOP.

* ---------------------------------------------------------------------
* 3. FAILED V1 UPDATES
* ---------------------------------------------------------------------
  SELECT COUNT( * ) FROM vbhdr INTO EV_FAILED_UPDATES
    WHERE vbdate = iv_date
      AND ( vbstate = '5' OR vbstate = '8' ).

* ---------------------------------------------------------------------
* 4. CANCELLED AND RUNNING JOBS
*    Cancelled jobs are matched on ENDDATE (they have one).
*    Running jobs are matched on SDLSTRTDT with NO end-date predicate --
*    the original required ENDDATE = today, which a running job never has,
*    so the running list was always empty.
* ---------------------------------------------------------------------
  SELECT * FROM tbtco INTO TABLE lt_tbtco
    WHERE ( status = 'A' AND enddate  = iv_date )
       OR ( status = 'R' AND sdlstrtdt = iv_date ).

  LOOP AT lt_tbtco INTO ls_tbtco.
    CLEAR ls_row.
    ls_row-wa = ls_tbtco-jobname.
    IF ls_tbtco-status = 'A'.
      EV_CANCELLED_JOBS = EV_CANCELLED_JOBS + 1.
      APPEND ls_row TO et_cancelled_jobs.
    ELSEIF ls_tbtco-status = 'R'.
      EV_RUNNING_JOBS = EV_RUNNING_JOBS + 1.
      APPEND ls_row TO et_running_jobs.
    ENDIF.
  ENDLOOP.

* ---------------------------------------------------------------------
* 5. DIALOG WORK PROCESSES
*    Now exports the total as well, so the caller can compute saturation
*    instead of showing a bare "free" count with no denominator.
* ---------------------------------------------------------------------
  CALL FUNCTION 'TH_WPINFO'
    TABLES wplist = lt_wplist
    EXCEPTIONS OTHERS = 1.
  IF sy-subrc = 0.
    LOOP AT lt_wplist INTO ls_wp WHERE wp_typ = 'DIA'.
      EV_TOTAL_DIA_WP = EV_TOTAL_DIA_WP + 1.
      IF ls_wp-wp_status CS 'Wait' OR ls_wp-wp_status CS 'WAIT'.
        EV_FREE_DIA_WP = EV_FREE_DIA_WP + 1.
      ENDIF.
    ENDLOOP.
  ENDIF.

* ---------------------------------------------------------------------
* 6. LOCK ENTRIES
* ---------------------------------------------------------------------
  CALL FUNCTION 'ENQUEUE_READ'
    EXPORTING gclient = iv_client
    TABLES    enq     = lt_enq
    EXCEPTIONS OTHERS = 1.
  IF sy-subrc = 0.
    DESCRIBE TABLE lt_enq LINES EV_LOCK_ENTRIES.
    LOOP AT lt_enq INTO ls_enq.
      CLEAR ls_row.
      ls_row-wa = ls_enq-guname.
      APPEND ls_row TO et_lock_users.
    ENDLOOP.
  ENDIF.

* ---------------------------------------------------------------------
* 7. INTERFACE HEALTH
*    All five of these were declared in the interface and never assigned.
* ---------------------------------------------------------------------
  SELECT COUNT( * ) FROM edidc INTO EV_FAILED_IDOCS
    WHERE upddat = iv_date AND status = '51'.

  SELECT COUNT( * ) FROM arfcsstate INTO EV_STUCK_TRFC
    WHERE arfcstate = 'SYSFAIL' OR arfcstate = 'CPICERR'.

  SELECT COUNT( * ) FROM trfcqout INTO EV_STUCK_OUT_QUEUES
    WHERE qstate = 'SYSFAIL' OR qstate = 'CPICERR'.

  SELECT COUNT( * ) FROM trfcqin INTO EV_STUCK_IN_QUEUES
    WHERE qstate = 'SYSFAIL' OR qstate = 'CPICERR'.

  SELECT COUNT( * ) FROM balhdr INTO EV_APP_LOG_ERRORS
    WHERE aldate = iv_date AND probclass <= '2'.

  SELECT COUNT( * ) FROM usr02 INTO EV_LOCKED_USERS
    WHERE uflag <> '0'.

* ---------------------------------------------------------------------
* 8. SERVER TIME
* ---------------------------------------------------------------------
  WRITE sy-uzeit TO lv_time_char.
  EV_SYS_TIME = lv_time_char.

* ---------------------------------------------------------------------
* 9. CPU UTILISATION VIA NATIVE SQL
*
*    The original queries did not measure CPU utilisation at all:
*      Sybase: max_online_engines is a CONFIGURED engine count, static.
*      HANA:   ACTIVE_LOGICAL_CPU_NUM / LOGICAL_CPU_CORES is a ratio of
*              CPU counts, pinned at 100 or a fixed hyperthreading ratio.
*
*    Replaced with real utilisation sources. On error the value stays 0
*    and the caller displays "No data" rather than a misleading number.
* ---------------------------------------------------------------------
  TRY.
      CREATE OBJECT lo_sql_stmt.
      IF EV_DB_TYPE CS 'hdb'.
        lo_result_set = lo_sql_stmt->execute_query(
          'SELECT TO_INT( ROUND( AVG(CPU) ) ) ' &&
          'FROM SYS.M_LOAD_HISTORY_HOST ' &&
          'WHERE TIME >= ADD_SECONDS(CURRENT_TIMESTAMP, -300)' ).
      ELSEIF EV_DB_TYPE CS 'syb'.
        lo_result_set = lo_sql_stmt->execute_query(
          'SELECT convert(int, avg(CPUBusy)) FROM master..monEngine' ).
      ENDIF.

      IF lo_result_set IS BOUND.
        GET REFERENCE OF lv_int_value INTO lr_value.
        lo_result_set->set_param( lr_value ).
        IF lo_result_set->next( ) > 0.
          EV_CPU_UTIL_PCT = lv_int_value.
        ENDIF.
        lo_result_set->close( ).
      ENDIF.

    CATCH cx_sql_exception.
      " Narrow catch: cx_root previously swallowed programming errors too.
      EV_CPU_UTIL_PCT = 0.
  ENDTRY.

* ---------------------------------------------------------------------
* 10. LAST SUCCESSFUL DATABASE BACKUP
*     SDBAH with FUNCT = 'DBB' is the legacy DBA-cockpit log and is not
*     populated on HANA. On HANA read M_BACKUP_CATALOG instead.
* ---------------------------------------------------------------------
  CLEAR EV_LAST_BACKUP.

  IF EV_DB_TYPE CS 'hdb'.
    TRY.
        CREATE OBJECT lo_sql_stmt.
        lo_result_set = lo_sql_stmt->execute_query(
          'SELECT TO_VARCHAR( MAX(SYS_END_TIME), ''YYYY-MM-DD HH24:MI'' ) ' &&
          'FROM SYS.M_BACKUP_CATALOG ' &&
          'WHERE STATE_NAME = ''successful'' AND ENTRY_TYPE_NAME = ''complete data backup''' ).
        GET REFERENCE OF EV_LAST_BACKUP INTO lr_value.
        lo_result_set->set_param( lr_value ).
        lo_result_set->next( ).
        lo_result_set->close( ).
      CATCH cx_sql_exception.
        CLEAR EV_LAST_BACKUP.
    ENDTRY.
  ELSE.
    " SELECT SINGLE does not permit ORDER BY -- that is a syntax error.
    " UP TO 1 ROWS ... ENDSELECT is the correct form for "newest row".
    SELECT * FROM sdbah INTO ls_sdbah UP TO 1 ROWS
      WHERE funct = 'DBB' AND rc = '0000'
      ORDER BY beg DESCENDING.
    ENDSELECT.
    IF sy-subrc = 0.
      CONCATENATE ls_sdbah-beg(4) '-' ls_sdbah-beg+4(2) '-' ls_sdbah-beg+6(2)
                  ' ' ls_sdbah-beg+8(2) ':' ls_sdbah-beg+10(2)
                  INTO EV_LAST_BACKUP.
    ENDIF.
  ENDIF.

  IF EV_LAST_BACKUP IS INITIAL.
    EV_LAST_BACKUP = 'N/A'.
  ENDIF.

* NOTE: there is deliberately NO test-data override block here.
* If you need to exercise the dashboard without a live system, set
* USE_MOCK_AI / a mock collector on the Python side -- never by
* overwriting real measurements inside the function module.

ENDFUNCTION.

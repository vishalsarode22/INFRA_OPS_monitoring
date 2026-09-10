FUNCTION z_get_logon_load.
*"----------------------------------------------------------------------
*"  Z_GET_LOGON_LOAD -- SMLG's instance load figures, over RFC.
*"
*"  IMPORTING
*"     VALUE(SRVNAME) TYPE MSNAME2 OPTIONAL
*"  TABLES
*"     INTG_TBL STRUCTURE RZLLIINTG
*"  EXCEPTIONS
*"     READ_FAILED
*"
*"  Attributes: Processing Type = Remote-Enabled Module.
*"  Function group: ZFG_OBS_MONITOR (or any Z group), package ZBASIS
*"  (or $TMP for a quick local test).
*"
*"  WHY THIS EXISTS
*"  Transaction SMLG's Resp.time / Users / Quality / Dialog-steps columns
*"  come from the message server's shared integer table, which SAPMSMLG
*"  (include MSMLGF02) reads with RZL_INTG_READALL_C, filtering records
*"  with VALUE1 = 7353. RZL_INTG_READALL_C is NOT remote-enabled on this
*"  kernel (probed 10.09.2026: FU_NOT_FOUND over RFC on CENTOR_QAS), so an
*"  external monitor cannot reach those figures directly. This wrapper
*"  makes the same call server-side and exports the rows unmodified.
*"
*"  Deliberately a dumb pass-through: no defaulting, no filtering. The
*"  caller (InfraBeatOps) supplies SRVNAME candidates itself -- message
*"  server first, then each server, then blank -- and filters VALUE1
*"  client-side. Nothing here to drift out of date.
*"
*"  Row semantics for VALUE1 = 7353 records (from the SAPMSMLG fill loop):
*"    NAME    application server        VALUE2  response time (ms)
*"    VALUE3  dialog steps              VALUE4  users
*"    VALUE5  quality                   VALUE   sample time (HHMMSS)
*"----------------------------------------------------------------------

  REFRESH intg_tbl.
  CALL FUNCTION 'RZL_INTG_READALL_C'
    EXPORTING
      srvname  = srvname
    TABLES
      intg_tbl = intg_tbl
    EXCEPTIONS
      OTHERS   = 1.
  IF sy-subrc <> 0.
    RAISE read_failed.
  ENDIF.

ENDFUNCTION.

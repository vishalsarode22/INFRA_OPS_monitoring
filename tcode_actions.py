"""
Per-T-code action sequences, translated from SAP GUI Script Recorder
recordings. Each function takes (session, capture) -- capture(suffix)
takes and saves a screenshot at that point, callable multiple times
for multi-checkpoint flows like ST22.
"""

import time

from sap_gui.field_extractor import get_grid_row_count, get_status_text
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from utils.logger import get_logger

log = get_logger(__name__, "application")


def _optional(session, wnd_id: str, fn, description: str = ""):
    try:
        obj = session.findById(wnd_id)
        fn(obj)
    except Exception as e:
        log.debug(f"Optional step skipped ({description or wnd_id}): {e}")


def action_al08(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "AL08")
    path = capture()

    session_summary = None
    if path:
        text = run_ocr(path)
        match = re.search(r"(\d+)\s*user logons with\s*(\d+)\s*back-end sessions", text, re.IGNORECASE)
        if match:
            user_logons = int(match.group(1))
            back_end_sessions = int(match.group(2))
            session_summary = f"{user_logons} user logons with {back_end_sessions} back-end sessions"
            return {
                "user_logons": user_logons,
                "back_end_sessions": back_end_sessions,
                "session_summary": session_summary,
            }

    return {}


def action_db01(session, capture):
    goto_tcode(session, "DB01")
    _optional(session, "wnd[0]/shellcont[1]/shell/shellcont[1]/shell",
              lambda o: setattr(o, "hierarchyHeaderWidth", 258), "DB01 header width")
    capture()


def action_db02(session, capture):
    goto_tcode(session, "DB02")
    _optional(session, "wnd[0]/shellcont[1]/shell/shellcont[1]/shell",
              lambda o: setattr(o, "hierarchyHeaderWidth", 258), "DB02 header width")
    try:
        tree = session.findById("wnd[0]/shellcont[1]/shell/shellcont[1]/shell")
        tree.expandNode("        100")
        tree.topNode = "        100"
        tree.selectItem("        101", "Task")
        tree.ensureVisibleHorizontalItem("        101", "Task")
        tree.doubleClickItem("        101", "Task")
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"DB02 tree navigation failed: {e}")
    capture()


def action_db12(session, capture):
    goto_tcode(session, "DB12")
    _optional(session, "wnd[0]/usr/cntlBACKUPCAT_ALV_CONTAINER/shellcont/shell",
              lambda o: setattr(o, "currentCellColumn", "SYS_END_TIME"), "DB12 sort column")
    capture()


def action_scot(session, capture):
    goto_tcode(session, "SCOT")
    try:
        tree = session.findById(
            "wnd[0]/usr/subCONTENT:SAPLSBCS_ADM:0104/subSUB_CONTENT:SAPLSBCS_NODES:0100/"
            "cntlSMTP_NODES_COLUMN_TREE_CONT/shellcont/shell"
        )
        tree.selectItem("SMTP", "Mail_Port")
        tree.ensureVisibleHorizontalItem("SMTP", "Mail_Port")
    except Exception as e:
        log.warning(f"SCOT SMTP node navigation failed: {e}")
    capture()


def action_sm12(session, capture):
    """
    SM12 supports two SAP GUI variants.

    Variant 1:
        Classic "Select Lock Entries"
        Uses:
            SM12
            txtSEQG3-GUNAME
            Execute btn[8]

    Variant 2:
        "Enqueue Administration"
        Uses:
            /NSM12
            ENQ_LOCK_FILTER-USERNAME
            ENQ_LOCK_FILTER-LIMIT
            btnLOAD

    The Enqueue Administration result is captured through
    the existing OCR pipeline.
    """

    # ================================================================
    # VARIANT 1: Classic "Select Lock Entries"
    # ================================================================
    try:
        log.info(
            "SM12: trying classic 'Select Lock Entries' variant."
        )

        goto_tcode(session, "SM12")

        user_field = session.findById(
            "wnd[0]/usr/txtSEQG3-GUNAME"
        )

        log.info(
            "SM12: classic GUNAME field found."
        )

        # Blank means all users.
        user_field.text = ""

        execute_button = session.findById(
            "wnd[0]/tbar[1]/btn[8]"
        )

        log.info(
            "SM12: classic Execute button found."
        )

        execute_button.press()

        wait_until_not_busy(session)

        path = capture()

        log.info(
            "SM12: classic variant executed successfully."
        )

        row_count = get_grid_row_count(
            session,
            "wnd[0]/usr/cntlGRID1/shellcont/"
            "shell/shellcont[1]/shell",
        )

        result = {
            "sm12_variant": "select_lock_entries",
        }

        if row_count is not None:
            result["lock_count"] = row_count

        return result

    except Exception as classic_error:
        log.warning(
            "SM12: classic variant unavailable/failed: %s: %s",
            type(classic_error).__name__,
            classic_error,
        )

    # ================================================================
    # VARIANT 2: Enqueue Administration
    # ================================================================
    log.info(
        "SM12: trying 'Enqueue Administration' variant."
    )

    # ------------------------------------------------
    # Navigate exactly like the working VBS:
    #
    # wnd[0]/tbar[0]/okcd = "/NSM12"
    # wnd[0].sendVKey 0
    # ------------------------------------------------
    command_field = session.findById(
        "wnd[0]/tbar[0]/okcd"
    )

    log.info(
        "SM12: command field found for Enqueue Administration."
    )

    command_field.text = "/NSM12"

    log.info(
        "SM12: entering /NSM12."
    )

    session.findById(
        "wnd[0]"
    ).sendVKey(0)

    log.info(
        "SM12: /NSM12 submitted."
    )

    wait_until_not_busy(session)

    import time
    time.sleep(1)

    # ------------------------------------------------
    # Enqueue Administration controls
    # ------------------------------------------------
    base = (
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
    )

    username_id = (
        base +
        "ctxtENQ_LOCK_FILTER-USERNAME"
    )

    limit_id = (
        base +
        "txtENQ_LOCK_FILTER-LIMIT"
    )

    load_id = (
        base +
        "btnLOAD"
    )

    # ------------------------------------------------
    # USERNAME = *
    # ------------------------------------------------
    username_field = session.findById(
        username_id
    )

    log.info(
        "SM12: Enqueue Administration USERNAME field found."
    )

    username_field.text = "*"

    log.info(
        "SM12: Enqueue Administration USERNAME set to '*'."
    )

    # ------------------------------------------------
    # LIMIT = blank
    # ------------------------------------------------
    limit_field = session.findById(
        limit_id
    )

    log.info(
        "SM12: Enqueue Administration LIMIT field found."
    )

    limit_field.text = ""

    log.info(
        "SM12: Enqueue Administration LIMIT cleared."
    )

    # ------------------------------------------------
    # LOAD / Search
    # ------------------------------------------------
    load_button = session.findById(
        load_id
    )

    log.info(
        "SM12: Enqueue Administration LOAD button found."
    )

    load_button.press()

    log.info(
        "SM12: Enqueue Administration LOAD pressed."
    )

    wait_until_not_busy(session)

    time.sleep(1)

    # ------------------------------------------------
    # Capture screenshot
    # ------------------------------------------------
    path = capture()

    log.info(
        "SM12: Enqueue Administration variant executed "
        "successfully. Screenshot=%s",
        path,
    )

    # ------------------------------------------------
    # OCR: Number Of Locks
    # ------------------------------------------------
    lock_count = None
    ocr_status = "not_detected"

    if path:
        try:
            from sap_gui.ocr_extractor import run_ocr
            import re

            text = run_ocr(path)

            log.info(
                "SM12: OCR completed for Enqueue Administration."
            )

            match = re.search(
                r"Number\s+Of\s+Locks\s+([0-9]+(?:[.,][0-9]+)?)",
                text,
                re.IGNORECASE,
            )

            if match:
                raw_value = match.group(1).strip()

                try:
                    normalized = raw_value.replace(",", ".")
                    numeric_value = float(normalized)

                    if numeric_value.is_integer():
                        lock_count = int(numeric_value)
                    else:
                        lock_count = numeric_value

                    ocr_status = "detected"

                    log.info(
                        "SM12: Number Of Locks detected by OCR: "
                        "%s (raw=%s)",
                        lock_count,
                        raw_value,
                    )

                except ValueError:
                    ocr_status = "parse_failed"

                    log.warning(
                        "SM12: OCR found Number Of Locks but "
                        "value could not be parsed: %s",
                        raw_value,
                    )

            else:
                log.warning(
                    "SM12: OCR could not find 'Number Of Locks'."
                )

        except Exception as ocr_error:
            ocr_status = "ocr_failed"

            log.warning(
                "SM12: OCR extraction failed: %s: %s",
                type(ocr_error).__name__,
                ocr_error,
            )

    return {
        "sm12_variant": "enqueue_administration",
        "lock_count": lock_count,
        "ocr_status": ocr_status,
    }
def action_sm13(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SM13")
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    update_summary = None
    if path:
        text = run_ocr(path)
        match = re.search(r"(\d+)\s*Update records?\s*found", text, re.IGNORECASE)
        if match:
            update_count = int(match.group(1))
            update_summary = f"{update_count} Update records found"
            return {"update_count": update_count, "update_summary": update_summary}

    return {}


def action_sm21(session, capture):
    goto_tcode(session, "SM21")
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    _optional(session, "wnd[0]/usr/cntlCONTAINER_0100/shellcont/shell/shellcont[1]/shell",
              lambda o: o.selectColumn("TEXT"), "SM21 select TEXT column")
    capture()


def action_sm37_active(session, capture):
    from sap_gui.ocr_extractor import run_ocr, count_time_prefixed_rows
    from datetime import datetime, timedelta

    goto_tcode(session, "SM37")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
    session.findById("wnd[0]/usr/ctxtBTCH2170-FROM_DATE").text = yesterday
    session.findById("wnd[0]/usr/chkBTCH2170-SCHEDUL").selected = False
    session.findById("wnd[0]/usr/chkBTCH2170-FINISHED").selected = False
    session.findById("wnd[0]/usr/chkBTCH2170-ABORTED").selected = False
    session.findById("wnd[0]/usr/txtBTCH2170-USERNAME").text = "*"
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    still_on_selection = True
    try:
        session.findById("wnd[0]/usr/chkBTCH2170-SCHEDUL")
    except Exception:
        still_on_selection = False

    if still_on_selection:
        return {"active_jobs": 0}

    job_count = count_time_prefixed_rows(run_ocr(path)) if path else None
    return {"active_jobs": job_count} if job_count is not None else {}

def action_sm37_cancelled(session, capture):
    from sap_gui.ocr_extractor import run_ocr, count_time_prefixed_rows
    from datetime import datetime

    goto_tcode(session, "SM37")
    today = datetime.now().strftime("%d.%m.%Y")
    session.findById("wnd[0]/usr/ctxtBTCH2170-FROM_DATE").text = today
    session.findById("wnd[0]/usr/chkBTCH2170-SCHEDUL").selected = False
    session.findById("wnd[0]/usr/chkBTCH2170-READY").selected = False
    session.findById("wnd[0]/usr/chkBTCH2170-RUNNING").selected = False
    session.findById("wnd[0]/usr/chkBTCH2170-FINISHED").selected = False
    session.findById("wnd[0]/usr/txtBTCH2170-USERNAME").text = "*"
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    still_on_selection = True
    try:
        session.findById("wnd[0]/usr/chkBTCH2170-SCHEDUL")
    except Exception:
        still_on_selection = False

    if still_on_selection:
        return {"cancelled_jobs": 0}

    job_count = count_time_prefixed_rows(run_ocr(path)) if path else None
    return {"cancelled_jobs": job_count} if job_count is not None else {}

def action_sm51(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SM51")
    grid_id = "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell/shellcont[1]/shell"
    _optional(session, grid_id, lambda o: o.selectAll(), "SM51 select all servers")
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)
    match = re.search(r"(\d+)\s*AS instance\(s\)\s*started", text, re.IGNORECASE)
    if match:
        return {"instances_started": int(match.group(1))}
    return {}


def action_sm58(session, capture):
    from sap_gui.ocr_extractor import run_ocr

    goto_tcode(session, "SM58")
    session.findById("wnd[0]/usr/txtBENUTZER-LOW").text = "*"
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)
    if "Nothing was selected" in text:
        return {"trfc_status": "Nothing was selected"}
    return {}


def action_sm66(session, capture):
    from sap_gui.ocr_extractor import run_ocr, count_occurrences
    import re

    goto_tcode(session, "SM66")
    session.findById("wnd[0]/tbar[1]/btn[13]").press()
    wait_until_not_busy(session)
    _optional(
        session,
        "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell/shellcont[1]/shell",
        lambda o: setattr(o, "currentCellColumn", "STATE_INFO_DISP"),
        "SM66 sort column",
    )
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)
    process_rows = len(re.findall(r"\d:\d{2}:\d{2}", text))
    running_count = count_occurrences(text, "Running")

    result = {}
    if process_rows:
        result["visible_process_rows"] = process_rows
    result["running_processes"] = running_count
    return result


def action_smlg(session, capture):
    goto_tcode(session, "SMLG")
    session.findById("wnd[0]/tbar[1]/btn[5]").press()
    wait_until_not_busy(session)
    capture()


def action_smq1(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SMQ1")
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    result = {}
    if path:
        text = run_ocr(path)
        if "Nothing selected" in text:
            result = {"entries_displayed": 0, "queues_displayed": 0}
        else:
            entries = re.search(r"Number of Entries Displayed:\s*(\d+)", text)
            queues = re.search(r"Number of Queues Displayed:\s*(\d+)", text)
            if entries:
                result["entries_displayed"] = int(entries.group(1))
            if queues:
                result["queues_displayed"] = int(queues.group(1))
    return result


def action_smq2(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SMQ2")
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    path = capture()

    result = {}
    if path:
        text = run_ocr(path)
        if "Nothing selected" in text:
            result = {"entries_displayed": 0, "queues_displayed": 0}
        else:
            entries = re.search(r"Number of Entries Displayed:\s*(\d+)", text)
            queues = re.search(r"Number of Queues Displayed:\s*(\d+)", text)
            if entries:
                result["entries_displayed"] = int(entries.group(1))
            if queues:
                result["queues_displayed"] = int(queues.group(1))
    return result

def action_sost(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SOST")
    try:
        base = ("wnd[0]/usr/subSUB:SAPLSBCS_OUT:1100/subTOPSUB:SAPLSBCS_OUT:1110/"
                 "tabsTAB1/tabpTAB1_FC1/ssubTAB1_SCA:SAPLSBCS_OUT:0003")
        session.findById(f"{base}/txtG_MAXSEL").text = "50000"
        session.findById(f"{base}/btnREFRICO2").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"SOST refresh step failed: {e}")
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)
    match = re.search(
        r"(\d+)\s*Send Requests\s*(\d+)\s*Waiting\s*(\d+)\s*Sent\s*(\d+)\s*Errors",
        text, re.IGNORECASE
    )
    if match:
        return {
            "send_requests": int(match.group(1)),
            "waiting": int(match.group(2)),
            "sent": int(match.group(3)),
            "errors": int(match.group(4)),
        }
    return {}


def action_sp01(session, capture):
    from sap_gui.ocr_extractor import run_ocr, count_spool_rows

    goto_tcode(session, "SP01")
    base = "wnd[0]/usr/tabsTABSTRIP_BL1/tabpSCR1/ssub%_SUBSCREEN_BL1:RSPOSP01NR:0100"
    session.findById(f"{base}/txtS_RQOWNE-LOW").text = "*"
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    _optional(session, "wnd[1]/usr/btnSEL2", lambda o: o.press(), "SP01 popup select-all")
    _optional(session, "wnd[0]/tbar[0]/btn[83]", lambda o: o.press(), "SP01 toolbar action")
    wait_until_not_busy(session)
    path = capture()

    spool_count = count_spool_rows(run_ocr(path)) if path else None
    return {"spool_count_visible": spool_count} if spool_count is not None else {}


def action_st03n(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "ST03N")
    try:
        tree = session.findById("wnd[0]/shellcont/shell/shellcont[1]/shell")
        tree.selectedNode = "B.999"
        tree.doubleClickNode("B.999")
        wait_until_not_busy(session)
    except Exception as e:
        log.warning(f"ST03N node navigation failed: {e}")
    _optional(session, "wnd[1]/usr/btnBUTTON_2", lambda o: o.press(), "ST03N confirm popup")
    wait_until_not_busy(session)
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)
    # Only target the DIALOG row's response time -- avoids parsing the
    # full dense table, which is unreliable for OCR alignment.
    match = re.search(r"DIALOG\s+(\d+)\s+([\d.,]+)", text)
    if match:
        return {"dialog_avg_response_time_ms": match.group(2)}
    return {}


def _extract_st22_alv_dumps(session, period: str):
    """Extract every visible ST22 dump row from the actual ALV grid.

    ST22 on this SAP GUI exposes the dump list through the RSSHOWRABAX ALV
    shell.  We use the scripting API instead of OCR so user/program/error/TID
    fields stay associated with the same row.
    """
    import re

    shell_id = (
        "wnd[0]/usr/cntlRSSHOWRABAX_ALV_100/"
        "shellcont/shell"
    )
    shell = session.findById(shell_id)

    row_count = int(getattr(shell, "RowCount", 0) or 0)
    columns = [
        "DATUM", "UZEIT", "AHOST", "UNAME", "MANDT",
        "XHOLD", "ERRORID", "REXCEPTION", "GPROGRAM", "MODNO", "TID",
    ]

    dumps = []
    for row in range(row_count):
        item = {}
        for column in columns:
            try:
                value = shell.GetCellValue(row, column)
            except Exception:
                value = ""
            item[column] = str(value or "").strip()

        # A real ST22 row has at least a date or time. Ignore blank/footer rows.
        if not item["DATUM"] and not item["UZEIT"]:
            continue

        dumps.append({
            "period": period,
            "date": item["DATUM"],
            "time": item["UZEIT"],
            "host": item["AHOST"],
            "user": item["UNAME"],
            "client": item["MANDT"],
            "hold_status": item["XHOLD"],
            "runtime_error": item["ERRORID"],
            "exception": item["REXCEPTION"],
            "program": item["GPROGRAM"],
            "module": item["MODNO"],
            "transaction_id": item["TID"],
        })

    return dumps


def action_st22_yesterday(session, capture):
    """Collect ST22 dumps for TODAY and YESTERDAY.

    Today is collected from the initial ST22 ALV.  Yesterday is collected by
    opening the standard Yesterday button.  Each dump is retained as a
    structured record; the primary dump metric remains today's count because
    the configured severity threshold is a per-day threshold.
    """
    from sap_gui.ocr_extractor import count_date_prefixed_lines, run_ocr

    goto_tcode(session, "ST22")

    # TODAY: ST22 opens directly on the current-day ALV in this SAP system.
    today_path = capture("today")
    today_dumps = []
    try:
        today_dumps = _extract_st22_alv_dumps(session, "today")
    except Exception as e:
        log.warning(f"ST22 today ALV extraction failed: {e}")
        # OCR fallback only supplies a count, never fake per-dump fields.
        if today_path:
            text = run_ocr(today_path)
            fallback_count = count_date_prefixed_lines(text)
        else:
            fallback_count = 0
    else:
        fallback_count = len(today_dumps)

    # YESTERDAY: use the recorded standard navigation.
    session.findById("wnd[0]/usr/btnYESTERD").press()
    wait_until_not_busy(session)
    yesterday_path = capture("yesterday")

    yesterday_dumps = []
    try:
        yesterday_dumps = _extract_st22_alv_dumps(session, "yesterday")
    except Exception as e:
        log.warning(f"ST22 yesterday ALV extraction failed: {e}")
        if yesterday_path:
            text = run_ocr(yesterday_path)
            yesterday_fallback_count = count_date_prefixed_lines(text)
        else:
            yesterday_fallback_count = 0
    else:
        yesterday_fallback_count = len(yesterday_dumps)

    # Return to the normal ST22 starting screen for the next T-code.
    try:
        session.findById("wnd[0]/tbar[0]/btn[3]").press()
        wait_until_not_busy(session)
    except Exception as e:
        log.debug(f"ST22 return-to-start skipped: {e}")

    all_dumps = today_dumps + yesterday_dumps

    # If scripting extraction failed, preserve accurate counts while keeping
    # the structured dump list empty rather than inventing row details.
    today_count = len(today_dumps) if today_dumps else fallback_count
    yesterday_count = len(yesterday_dumps) if yesterday_dumps else yesterday_fallback_count

    users = sorted({d["user"] for d in all_dumps if d.get("user")})
    programs = sorted({d["program"] for d in all_dumps if d.get("program")})
    errors = sorted({d["runtime_error"] for d in all_dumps if d.get("runtime_error")})

    return {
        "dump_count": today_count,
        "today_dump_count": today_count,
        "yesterday_dump_count": yesterday_count,
        "dumps": all_dumps,
        "today_dumps": today_dumps,
        "yesterday_dumps": yesterday_dumps,
        "unique_users": users,
        "unique_programs": programs,
        "unique_runtime_errors": errors,
    }


ACTIONS = {
    "al08": action_al08,
    "db01": action_db01,
    "db02": action_db02,
    "db12": action_db12,
    "scot": action_scot,
    "sm12": action_sm12,
    "sm13": action_sm13,
    "sm21": action_sm21,
    "sm37_active": action_sm37_active,
    "sm37_cancelled": action_sm37_cancelled,
    "sm51": action_sm51,
    "sm58": action_sm58,
    "sm66": action_sm66,
    "smlg": action_smlg,
    "smq1": action_smq1,
    "smq2": action_smq2,
    "sost": action_sost,
    "sp01": action_sp01,
    "st03n": action_st03n,
    "st22_yesterday": action_st22_yesterday,
}
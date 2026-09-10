"""
Per-T-code action sequences, translated from SAP GUI Script Recorder
recordings. Each function takes (session, capture) -- capture(suffix)
takes and saves a screenshot at that point, callable multiple times
for multi-checkpoint flows like ST22.
"""

import time
import re

from sap_gui.field_extractor import get_grid_row_count, get_status_text
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from sap_gui.sm50_extractor import extract_sm50_rows
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
        match = re.search(
            r"(\d+)\s*user logons with\s*(\d+)\s*back-end sessions",
            text,
            re.IGNORECASE,
        )
        if match:
            user_logons = int(match.group(1))
            back_end_sessions = int(match.group(2))
            session_summary = (
                f"{user_logons} user logons with "
                f"{back_end_sessions} back-end sessions"
            )
            return {
                "user_logons": user_logons,
                "back_end_sessions": back_end_sessions,
                "session_summary": session_summary,
            }

    return {}


def action_db01(session, capture):
    goto_tcode(session, "DB01")
    _optional(
        session,
        "wnd[0]/shellcont[1]/shell/shellcont[1]/shell",
        lambda o: setattr(o, "hierarchyHeaderWidth", 258),
        "DB01 header width",
    )
    capture()


def action_db02(session, capture):
    goto_tcode(session, "DB02")
    _optional(
        session,
        "wnd[0]/shellcont[1]/shell/shellcont[1]/shell",
        lambda o: setattr(o, "hierarchyHeaderWidth", 258),
        "DB02 header width",
    )
    try:
        tree = session.findById(
            "wnd[0]/shellcont[1]/shell/shellcont[1]/shell"
        )
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
    _optional(
        session,
        "wnd[0]/usr/cntlBACKUPCAT_ALV_CONTAINER/shellcont/shell",
        lambda o: setattr(o, "currentCellColumn", "SYS_END_TIME"),
        "DB12 sort column",
    )
    capture()


def action_scot(session, capture):
    goto_tcode(session, "SCOT")
    try:
        tree = session.findById(
            "wnd[0]/usr/subCONTENT:SAPLSBCS_ADM:0104/"
            "subSUB_CONTENT:SAPLSBCS_NODES:0100/"
            "cntlSMTP_NODES_COLUMN_TREE_CONT/shellcont/shell"
        )
        tree.selectItem("SMTP", "Mail_Port")
        tree.ensureVisibleHorizontalItem("SMTP", "Mail_Port")
    except Exception as e:
        log.warning(f"SCOT SMTP node navigation failed: {e}")
    capture()


def action_sm12(session, capture):
    """
    SM12 supports different SAP GUI variants depending on the
    SAP release/system configuration.

    Variant 1:
        Classic "Select Lock Entries"

    Variant 2:
        "Enqueue Administration"

    Try the classic recorded flow first. If its controls are not
    available, fall back to the Enqueue Administration flow.
    """

    from sap_gui.ocr_extractor import run_ocr
    import re

    # ============================================================
    # Variant 1: Classic "Select Lock Entries"
    # ============================================================
    try:
        log.info("SM12: trying classic 'Select Lock Entries' variant.")

        goto_tcode(session, "SM12")

        # Blank username = all users
        session.findById(
            "wnd[0]/usr/txtSEQG3-GUNAME"
        ).text = ""

        # Execute
        session.findById(
            "wnd[0]/tbar[1]/btn[8]"
        ).press()

        wait_until_not_busy(session)

        path = capture()

        row_count = get_grid_row_count(
            session,
            "wnd[0]/usr/cntlGRID1/shellcont/"
            "shell/shellcont[1]/shell",
        )

        log.info(
            "SM12: classic 'Select Lock Entries' variant "
            "executed successfully."
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

    # ============================================================
    # Variant 2: Enqueue Administration
    # ============================================================
    log.info("SM12: trying 'Enqueue Administration' variant.")

    # SAP GUI command field
    okcd = session.findById(
        "wnd[0]/tbar[0]/okcd"
    )

    log.info("SM12: command field found for Enqueue Administration.")

    okcd.text = "/NSM12"

    log.info("SM12: entering /NSM12.")

    session.findById(
        "wnd[0]"
    ).sendVKey(0)

    wait_until_not_busy(session)

    log.info("SM12: /NSM12 submitted.")

    # ------------------------------------------------------------
    # Enqueue Administration fields
    # ------------------------------------------------------------

    username_field = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "ctxtENQ_LOCK_FILTER-USERNAME"
    )

    log.info(
        "SM12: Enqueue Administration USERNAME field found."
    )

    username_field.text = "*"

    log.info(
        "SM12: Enqueue Administration USERNAME set to '*'."
    )

    limit_field = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "txtENQ_LOCK_FILTER-LIMIT"
    )

    log.info(
        "SM12: Enqueue Administration LIMIT field found."
    )

    limit_field.text = ""

    log.info(
        "SM12: Enqueue Administration LIMIT cleared."
    )

    load_button = session.findById(
        "wnd[0]/usr/"
        "subAREA_TOP:RS_ENQ_ADMIN:0111/"
        "btnLOAD"
    )

    log.info(
        "SM12: Enqueue Administration LOAD button found."
    )

    load_button.press()

    log.info(
        "SM12: Enqueue Administration LOAD pressed."
    )

    wait_until_not_busy(session)

    path = capture()

    log.info(
        "SM12: Enqueue Administration variant executed "
        "successfully. Screenshot=%s",
        path,
    )

    result = {
        "sm12_variant": "enqueue_administration",
    }

    # ============================================================
    # Extract lock count from Enqueue Administration screenshot
    # ============================================================

    if path:
        try:
            text = run_ocr(path)

            # Enqueue Administration displays the result as:
            #
            #     Lock Table (0)
            #
            #     Lock Table (1)
            #
            #     Lock Table (25)
            #
            # The actual lock count is contained in the
            # "Lock Table (...)" result, not immediately after
            # the "Number Of Locks" search-criteria label.

            match = re.search(
                r"Lock\s+Table\s*\(\s*([0-9]+)\s*\)",
                text,
                re.IGNORECASE,
            )

            if match:
                result["lock_count"] = int(match.group(1))

                log.info(
                    "SM12: Lock Table count = %s",
                    result["lock_count"],
                )
            else:
                log.warning(
                    "SM12: Lock Table count was not detected by OCR."
                )

        except Exception as ocr_error:
            log.warning(
                "SM12: OCR extraction failed: %s: %s",
                type(ocr_error).__name__,
                ocr_error,
            )

    # IMPORTANT:
    # The Enqueue Administration branch must return the result.
    # Without this return, Python returns None and the collector
    # converts it to {}, losing lock_count from evidence.
    return result


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
        match = re.search(
            r"(\d+)\s*Update records?\s*found",
            text,
            re.IGNORECASE,
        )
        if match:
            update_count = int(match.group(1))
            update_summary = f"{update_count} Update records found"
            return {
                "update_count": update_count,
                "update_summary": update_summary,
            }

    return {}


def action_sm21(session, capture):
    goto_tcode(session, "SM21")
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)
    _optional(
        session,
        "wnd[0]/usr/cntlCONTAINER_0100/shellcont/"
        "shell/shellcont[1]/shell",
        lambda o: o.selectColumn("TEXT"),
        "SM21 select TEXT column",
    )
    capture()


def action_sm37_active(session, capture):
    """
    SM37 Active Jobs monitoring.

    Monitoring rule:
        Date range : TODAY -> TODAY
        Status     : RUNNING only
        User       : *

    A valid SAP response of:
        "No job matches the selection criteria"

    means there are zero active jobs. It is NOT a collection failure.
    """

    from sap_gui.ocr_extractor import run_ocr, count_time_prefixed_rows
    from datetime import datetime
    import re

    goto_tcode(session, "SM37")

    today = datetime.now().strftime("%d.%m.%Y")

    # ---------------------------------------------------------------
    # Date range: TODAY -> TODAY
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/usr/ctxtBTCH2170-FROM_DATE"
    ).text = today

    session.findById(
        "wnd[0]/usr/ctxtBTCH2170-TO_DATE"
    ).text = today

    # ---------------------------------------------------------------
    # Status: RUNNING only
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/usr/chkBTCH2170-PRELIM"
    ).selected = False

    session.findById(
        "wnd[0]/usr/chkBTCH2170-SCHEDUL"
    ).selected = False

    session.findById(
        "wnd[0]/usr/chkBTCH2170-READY"
    ).selected = False

    session.findById(
        "wnd[0]/usr/chkBTCH2170-RUNNING"
    ).selected = True

    session.findById(
        "wnd[0]/usr/chkBTCH2170-FINISHED"
    ).selected = False

    session.findById(
        "wnd[0]/usr/chkBTCH2170-ABORTED"
    ).selected = False

    # ---------------------------------------------------------------
    # All users
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/usr/txtBTCH2170-USERNAME"
    ).text = "*"

    # ---------------------------------------------------------------
    # Execute
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # Read SAP status message
    # ---------------------------------------------------------------

    status_text = ""

    try:
        status_text = (
            session.findById("wnd[0]/sbar").Text or ""
        ).strip()
    except Exception as e:
        log.debug(
            "SM37 active: could not read status bar: %s",
            e,
        )

    log.info(
        "SM37 active: SAP status = %r",
        status_text,
    )

    # ---------------------------------------------------------------
    # Valid zero-result condition
    # ---------------------------------------------------------------

    if re.search(
        r"no\s+job\s+matches\s+the\s+selection\s+criteria",
        status_text,
        re.IGNORECASE,
    ):
        log.info(
            "SM37 active: no running jobs found for %s.",
            today,
        )

        path = capture()

        return {
            "active_jobs": 0,
            "active_jobs_date_from": today,
            "active_jobs_date_to": today,
            "active_jobs_status": "RUNNING",
        }

    # ---------------------------------------------------------------
    # Capture result screen
    # ---------------------------------------------------------------

    path = capture()

    if not path:
        log.warning(
            "SM37 active: result screen reached but screenshot "
            "capture failed."
        )
        return {}

    # ---------------------------------------------------------------
    # Verify that we left the selection screen
    # ---------------------------------------------------------------

    still_on_selection = False

    try:
        session.findById(
            "wnd[0]/usr/chkBTCH2170-SCHEDUL"
        )
        still_on_selection = True
    except Exception:
        still_on_selection = False

    if still_on_selection:
        log.warning(
            "SM37 active: still on selection screen. "
            "SAP status was not a recognized zero-result message: %r",
            status_text,
        )

        return {}

    # ---------------------------------------------------------------
    # Extract visible job rows from result screen
    # ---------------------------------------------------------------

    text = run_ocr(path)

    job_count = count_time_prefixed_rows(text)

    if job_count is not None:
        log.info(
            "SM37 active: %s running job row(s) detected.",
            job_count,
        )

        return {
            "active_jobs": job_count,
            "active_jobs_date_from": today,
            "active_jobs_date_to": today,
            "active_jobs_status": "RUNNING",
        }

    log.warning(
        "SM37 active: result screen captured but active job "
        "count could not be extracted."
    )

    return {}


def action_sm37_cancelled(session, capture):
    """
    SM37 cancelled-job monitoring.

    Monitoring window:
        FROM = yesterday
        TO   = today

    Only CANCELED jobs are selected.

    IMPORTANT:
        The SM37 result screen on this SAP system does not expose an
        ALV/Grid control. The visible result rows are exposed directly
        as GuiLabel controls with positional IDs such as:

            lbl[4,13]    -> Job Name
            lbl[51,13]   -> Job Created By
            lbl[64,13]   -> Status
            lbl[80,13]   -> Start Date
            lbl[91,13]   -> Start Time
            lbl[102,13]  -> Duration
            lbl[117,13]  -> Delay
            lbl[123,13]  -> Client

        Therefore we read the SAP GUI scripting values directly instead
        of using OCR.

    Returns:
        {
            "cancelled_jobs": <int>,
            "cancelled_job_rows": [...]
        }
    """

    from datetime import datetime, timedelta
    import re

    goto_tcode(session, "SM37")

    # ---------------------------------------------------------------
    # Date range: yesterday -> today
    # ---------------------------------------------------------------

    today = datetime.now()
    yesterday = today - timedelta(days=1)

    from_date = yesterday.strftime("%d.%m.%Y")
    to_date = today.strftime("%d.%m.%Y")

    log.info(
        "SM37 cancelled: searching from %s to %s.",
        from_date,
        to_date,
    )

    session.findById(
        "wnd[0]/usr/ctxtBTCH2170-FROM_DATE"
    ).text = from_date

    session.findById(
        "wnd[0]/usr/ctxtBTCH2170-TO_DATE"
    ).text = to_date

    # ---------------------------------------------------------------
    # Job/user filters
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/usr/txtBTCH2170-USERNAME"
    ).text = "*"

    # ---------------------------------------------------------------
    # Status selection
    #
    # Only CANCELED should be selected.
    # ---------------------------------------------------------------

    status_fields = {
        "PRELIM": "wnd[0]/usr/chkBTCH2170-PRELIM",
        "SCHEDUL": "wnd[0]/usr/chkBTCH2170-SCHEDUL",
        "READY": "wnd[0]/usr/chkBTCH2170-READY",
        "RUNNING": "wnd[0]/usr/chkBTCH2170-RUNNING",
        "FINISHED": "wnd[0]/usr/chkBTCH2170-FINISHED",
        "ABORTED": "wnd[0]/usr/chkBTCH2170-ABORTED",
    }

    for name, control_id in status_fields.items():
        session.findById(control_id).selected = False

    # This is the important status.
    session.findById(
        "wnd[0]/usr/chkBTCH2170-ABORTED"
    ).selected = True

    # ---------------------------------------------------------------
    # IMPORTANT:
    #
    # On this SAP system the visible checkbox is labelled "Canceled",
    # while the scripting field is BTCH2170-ABORTED.
    #
    # Verify the actual selected state before execution.
    # ---------------------------------------------------------------

    aborted_selected = session.findById(
        "wnd[0]/usr/chkBTCH2170-ABORTED"
    ).selected

    log.info(
        "SM37 cancelled: Canceled/ABORTED selection = %s",
        aborted_selected,
    )

    # ---------------------------------------------------------------
    # Execute
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)

    # Small rendering delay. SM37 can finish its backend operation
    # before all GuiLabel objects have been populated.
    import time
    time.sleep(0.5)

    # ---------------------------------------------------------------
    # Check SAP status bar first.
    # ---------------------------------------------------------------

    sap_status = ""

    try:
        sap_status = (
            session.findById("wnd[0]/sbar").Text or ""
        ).strip()
    except Exception as e:
        log.debug(
            "SM37 cancelled: could not read status bar: %s",
            e,
        )

    log.info(
        "SM37 cancelled: SAP status = '%s'",
        sap_status,
    )

    # ---------------------------------------------------------------
    # Capture evidence regardless of whether jobs exist.
    # ---------------------------------------------------------------

    path = capture()

    # ---------------------------------------------------------------
    # Explicit no-result condition.
    # ---------------------------------------------------------------

    if (
        "No job matches the selection criteria"
        in sap_status
    ):
        log.info(
            "SM37 cancelled: no canceled jobs found "
            "between %s and %s.",
            from_date,
            to_date,
        )

        return {
            "cancelled_jobs": 0,
            "cancelled_job_rows": [],
            "cancelled_from_date": from_date,
            "cancelled_to_date": to_date,
            "sap_status": sap_status,
        }

    # ---------------------------------------------------------------
    # Read GuiLabel result rows directly.
    # ---------------------------------------------------------------

    try:
        usr = session.findById("wnd[0]/usr")
    except Exception as e:
        log.warning(
            "SM37 cancelled: could not access result user area: %s",
            e,
        )

        return {
            "cancelled_jobs": 0,
            "cancelled_job_rows": [],
            "cancelled_from_date": from_date,
            "cancelled_to_date": to_date,
            "sap_status": sap_status,
        }

    # ---------------------------------------------------------------
    # Build:
    #
    #     row_number -> column_position -> text
    #
    # Example:
    #
    #     row 13:
    #       4   = DBA:DATABACKUP...
    #       51  = BASIS
    #       64  = Canceled
    #       80  = 20.08.2026
    #       91  = 00:00:32
    # ---------------------------------------------------------------

    rows = {}

    for i in range(usr.Children.Count):

        try:
            control = usr.Children(i)

            if getattr(control, "Type", "") != "GuiLabel":
                continue

            control_id = getattr(control, "Id", "")
            text = getattr(control, "Text", "")

            if not text:
                continue

            text = text.strip()

            if not text:
                continue

            # SAP GUI IDs look like:
            #
            # /usr/lbl[4,13]
            #
            match = re.search(
                r"/lbl\[(\d+),(\d+)\]$",
                control_id,
            )

            if not match:
                continue

            column = int(match.group(1))
            row_number = int(match.group(2))

            rows.setdefault(row_number, {})
            rows[row_number][column] = text

        except Exception as e:
            log.debug(
                "SM37 cancelled: failed reading child control: %s",
                e,
            )

    # ---------------------------------------------------------------
    # Convert SAP rows into structured job records.
    # ---------------------------------------------------------------

    cancelled_job_rows = []

    for row_number in sorted(rows):

        row = rows[row_number]

        job_name = row.get(4, "")
        created_by = row.get(51, "")
        status = row.get(64, "")
        start_date = row.get(80, "")
        start_time = row.get(91, "")
        duration = row.get(102, "")
        delay = row.get(117, "")
        client = row.get(123, "")

        # -----------------------------------------------------------
        # Ignore:
        #
        # - header row
        # - summary row
        # - unrelated labels
        # - rows without a job name
        # -----------------------------------------------------------

        if not job_name:
            continue

        if job_name.lower() in {
            "jobname",
            "summary",
            "*summary",
        }:
            continue

        # The actual job result must explicitly say Canceled.
        if status.strip().lower() != "canceled":
            continue

        cancelled_job_rows.append(
            {
                "job_name": job_name,
                "created_by": created_by,
                "status": status,
                "start_date": start_date,
                "start_time": start_time,
                "duration_seconds": duration.strip(),
                "delay_seconds": delay.strip(),
                "client": client,
            }
        )

    cancelled_count = len(cancelled_job_rows)

    log.info(
        "SM37 cancelled: %s canceled job row(s) detected.",
        cancelled_count,
    )

    # ---------------------------------------------------------------
    # Log individual jobs for traceability.
    # ---------------------------------------------------------------

    for index, job in enumerate(cancelled_job_rows, start=1):

        log.info(
            "SM37 cancelled job %s: "
            "name='%s', user='%s', date='%s', time='%s', "
            "duration='%s', delay='%s', client='%s'",
            index,
            job["job_name"],
            job["created_by"],
            job["start_date"],
            job["start_time"],
            job["duration_seconds"],
            job["delay_seconds"],
            job["client"],
        )

    return {
        "cancelled_jobs": cancelled_count,
        "cancelled_job_rows": cancelled_job_rows,
        "cancelled_from_date": from_date,
        "cancelled_to_date": to_date,
        "sap_status": sap_status,
    }


def action_sm51(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SM51")

    grid_id = (
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell/shellcont[1]/shell"
    )

    _optional(
        session,
        grid_id,
        lambda o: o.selectAll(),
        "SM51 select all servers",
    )

    path = capture()

    if not path:
        return {}

    text = run_ocr(path)

    match = re.search(
        r"(\d+)\s*AS instance\(s\)\s*started",
        text,
        re.IGNORECASE,
    )

    if match:
        return {
            "instances_started": int(match.group(1))
        }

    return {}


def action_sm58(session, capture):
    from sap_gui.ocr_extractor import run_ocr

    goto_tcode(session, "SM58")

    session.findById(
        "wnd[0]/usr/txtBENUTZER-LOW"
    ).text = "*"

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)
    path = capture()

    if not path:
        return {}

    text = run_ocr(path)

    if "Nothing was selected" in text:
        return {
            "trfc_status": "Nothing was selected"
        }

    return {}

def action_sm50(session, capture):
    """
    Collect structured SM50 work-process data through SAP GUI Scripting.

    The screenshot is retained as visual evidence, but monitoring analysis
    uses the SAP grid's technical column identifiers directly.
    """

    goto_tcode(session, "SM50")
    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # SM50 work-process grid
    # ---------------------------------------------------------------

    grid = session.findById(
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell"
    )

    columns = {
        "wp_index": "WP_INDEX",
        "wp_type": "WP_TYPE_DISP",
        "pid": "PID",
        "state": "STATE_DISP",
        "state_info": "STATE_INFO_DISP",
        "failures": "FAILURES",
        "sem_locked": "SEM_LOCKED",
        "sem_locking": "SEM_LOCKING",
        "cpu": "CPU",
        "elapsed_time": "ELAPSED_TIME",
        "priority": "PRIORITY_DISP",
        "wait_priority": "WAIT_FOR_PRIORITY_DISP",
        "program": "WP_PROGRAM",
        "client": "TENANT_DISP",
        "user": "USER_NAME",
        "current_action": "CURRENT_ACTION_DISP",
        "action_info": "ACTION_INFO",
    }

    def cell(row, column):
        try:
            value = grid.GetCellValue(row, column)

            if value is None:
                return ""

            return str(value).strip()

        except Exception:
            return ""

    processes = []

    for row in range(int(grid.RowCount)):
        process = {
            key: cell(row, technical_name)
            for key, technical_name in columns.items()
        }

        # Ignore completely empty rows.
        if not any(process.values()):
            continue

        processes.append(process)

    # ---------------------------------------------------------------
    # Derived work-process health
    # ---------------------------------------------------------------

    state_counts = {}

    for process in processes:
        state = process.get("state") or "UNKNOWN"
        state_counts[state] = state_counts.get(state, 0) + 1

    running = state_counts.get("Running", 0)
    waiting = state_counts.get("Waiting", 0)
    stopped = state_counts.get("Stopped", 0)
    held = state_counts.get("Held", 0)

    # ---------------------------------------------------------------
    # Screenshot remains visual evidence
    # ---------------------------------------------------------------

    screenshot_path = capture()

    result = {
        "total_work_processes": len(processes),
        "running_processes": running,
        "waiting_processes": waiting,
        "stopped_processes": stopped,
        "held_processes": held,
        "state_counts": state_counts,
        "processes": processes,
    }

    if screenshot_path:
        result["screenshot_captured"] = True

    return result

def action_sm66(session, capture):
    from sap_gui.ocr_extractor import run_ocr, count_occurrences
    import re

    goto_tcode(session, "SM66")

    session.findById(
        "wnd[0]/tbar[1]/btn[13]"
    ).press()

    wait_until_not_busy(session)

    _optional(
        session,
        "wnd[0]/usr/cntlGRID1/shellcont/shell/"
        "shellcont[1]/shell/shellcont[1]/shell",
        lambda o: setattr(
            o,
            "currentCellColumn",
            "STATE_INFO_DISP",
        ),
        "SM66 sort column",
    )

    path = capture()

    if not path:
        return {}

    text = run_ocr(path)

    process_rows = len(
        re.findall(r"\d:\d{2}:\d{2}", text)
    )

    running_count = count_occurrences(
        text,
        "Running",
    )

    result = {}

    if process_rows:
        result["visible_process_rows"] = process_rows

    result["running_processes"] = running_count

    return result


def action_smlg(session, capture):
    """
    Collect SMLG load-distribution / instance response-time data.

    SMLG renders the load-distribution overview as GuiLabel controls
    rather than an ALV grid.

    The response-time overview contains:
        Instance
        State
        Resp.time(ms)
        Thrshd
        User
        Thrshd
        Time
        Quality
        Dialog steps

    Only rows containing a response-time value are treated as real
    SMLG instance rows. Summary and logon-group information is ignored.
    """

    import re

    goto_tcode(session, "SMLG")
    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # Open SMLG Load Distribution / Overview
    # ---------------------------------------------------------------

    session.findById(
        "wnd[0]/tbar[1]/btn[5]"
    ).press()

    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # Read SMLG screen labels
    # ---------------------------------------------------------------

    user_area = session.findById("wnd[0]/usr")

    labels = {}

    for i in range(int(user_area.Children.Count)):
        obj = user_area.Children(i)

        try:
            control_id = obj.Id
            text = getattr(obj, "Text", "") or ""

            # Expected format:
            # /lbl[1,3]
            # /lbl[21,3]
            # /lbl[71,5]
            match = re.search(
                r"/lbl\[(\d+),(\d+)\]$",
                control_id,
            )

            if not match:
                continue

            column = int(match.group(1))
            row = int(match.group(2))

            labels[(column, row)] = str(text).strip()

        except Exception:
            # A single inaccessible SAP GUI control must not
            # invalidate the complete SMLG collection.
            continue

    # ---------------------------------------------------------------
    # Extract instance rows
    # ---------------------------------------------------------------

    instances = []

    # SMLG uses rows 3, 5, 7, ... for the instance overview.
    for row in range(3, 100, 2):

        instance = labels.get((1, row), "").strip()

        if not instance:
            continue

        state = labels.get((15, row), "").strip()
        response_raw = labels.get((21, row), "").strip()
        threshold = labels.get((35, row), "").strip()
        user_count = labels.get((42, row), "").strip()
        user_threshold = labels.get((47, row), "").strip()
        time_value = labels.get((54, row), "").strip()
        quality = labels.get((63, row), "").strip()
        dialog_steps = labels.get((71, row), "").strip()

        # -----------------------------------------------------------
        # Only accept rows that actually contain response time.
        #
        # This prevents rows such as:
        #   * Summary
        #   Logon Group
        #   SAPERP
        #
        # from being treated as SAP application instances.
        # -----------------------------------------------------------

        if not response_raw:
            continue

        if instance.startswith("*"):
            continue

        # -----------------------------------------------------------
        # Convert response time to milliseconds
        # -----------------------------------------------------------

        response_time_ms = None

        match = re.search(
            r"\d+(?:[.,]\d+)?",
            response_raw,
        )

        if match:
            try:
                response_time_ms = float(
                    match.group(0).replace(",", ".")
                )
            except (ValueError, TypeError):
                response_time_ms = None

        # Do not include a row if response time could not be parsed.
        if response_time_ms is None:
            continue

        instances.append(
            {
                "instance": instance,
                "state": state,
                "response_time_ms": response_time_ms,
                "response_time_raw": response_raw,
                "threshold": threshold,
                "user_count": user_count,
                "user_threshold": user_threshold,
                "time": time_value,
                "quality": quality,
                "dialog_steps": dialog_steps,
            }
        )

    # ---------------------------------------------------------------
    # Derived response-time statistics
    # ---------------------------------------------------------------

    response_times = [
        item["response_time_ms"]
        for item in instances
        if item.get("response_time_ms") is not None
    ]

    result = {
        "instance_count": len(instances),
        "instances": instances,
    }

    if response_times:
        result["max_response_time_ms"] = max(response_times)

        result["avg_response_time_ms"] = (
            sum(response_times) / len(response_times)
        )

        # Instance having the highest observed response time.
        worst_instance = max(
            instances,
            key=lambda item: item["response_time_ms"],
        )

        result["worst_instance"] = {
            "instance": worst_instance["instance"],
            "response_time_ms": worst_instance["response_time_ms"],
        }

    # ---------------------------------------------------------------
    # Screenshot remains visual evidence
    # ---------------------------------------------------------------

    screenshot_path = capture()

    if screenshot_path:
        result["screenshot_captured"] = True

    return result

def action_smq1(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SMQ1")

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)
    path = capture()

    result = {}

    if path:
        text = run_ocr(path)

        if "Nothing selected" in text:
            result = {
                "entries_displayed": 0,
                "queues_displayed": 0,
            }
        else:
            entries = re.search(
                r"Number of Entries Displayed:\s*(\d+)",
                text,
            )

            queues = re.search(
                r"Number of Queues Displayed:\s*(\d+)",
                text,
            )

            if entries:
                result["entries_displayed"] = int(
                    entries.group(1)
                )

            if queues:
                result["queues_displayed"] = int(
                    queues.group(1)
                )

    return result


def action_smq2(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SMQ2")

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)
    path = capture()

    result = {}

    if path:
        text = run_ocr(path)

        if "Nothing selected" in text:
            result = {
                "entries_displayed": 0,
                "queues_displayed": 0,
            }
        else:
            entries = re.search(
                r"Number of Entries Displayed:\s*(\d+)",
                text,
            )

            queues = re.search(
                r"Number of Queues Displayed:\s*(\d+)",
                text,
            )

            if entries:
                result["entries_displayed"] = int(
                    entries.group(1)
                )

            if queues:
                result["queues_displayed"] = int(
                    queues.group(1)
                )

    return result


def action_sost(session, capture):
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "SOST")

    try:
        base = (
            "wnd[0]/usr/subSUB:SAPLSBCS_OUT:1100/"
            "subTOPSUB:SAPLSBCS_OUT:1110/"
            "tabsTAB1/tabpTAB1_FC1/"
            "ssubTAB1_SCA:SAPLSBCS_OUT:0003"
        )

        session.findById(
            f"{base}/txtG_MAXSEL"
        ).text = "50000"

        session.findById(
            f"{base}/btnREFRICO2"
        ).press()

        wait_until_not_busy(session)

    except Exception as e:
        log.warning(
            f"SOST refresh step failed: {e}"
        )

    path = capture()

    if not path:
        return {}

    text = run_ocr(path)

    match = re.search(
        r"(\d+)\s*Send Requests\s*"
        r"(\d+)\s*Waiting\s*"
        r"(\d+)\s*Sent\s*"
        r"(\d+)\s*Errors",
        text,
        re.IGNORECASE,
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

    base = (
        "wnd[0]/usr/tabsTABSTRIP_BL1/tabpSCR1/"
        "ssub%_SUBSCREEN_BL1:RSPOSP01NR:0100"
    )

    session.findById(
        f"{base}/txtS_RQOWNE-LOW"
    ).text = "*"

    session.findById(
        "wnd[0]/tbar[1]/btn[8]"
    ).press()

    wait_until_not_busy(session)

    _optional(
        session,
        "wnd[1]/usr/btnSEL2",
        lambda o: o.press(),
        "SP01 popup select-all",
    )

    _optional(
        session,
        "wnd[0]/tbar[0]/btn[83]",
        lambda o: o.press(),
        "SP01 toolbar action",
    )

    wait_until_not_busy(session)

    path = capture()

    spool_count = (
        count_spool_rows(run_ocr(path))
        if path
        else None
    )

    return (
        {"spool_count_visible": spool_count}
        if spool_count is not None
        else {}
    )


def action_st06(session, capture):
    """
    OS monitor screenshot plus the memory and paging figures.

    ST06 is the authority for physical memory on the host. SMON exports free
    memory but not the total, and deriving the total from free MB and free
    percent is unreliable because the percent is rounded to whole numbers --
    on a 15,643 MB host reading 1% free, that derivation returned ~25,000 MB.

    Page Out is the figure that matters most here. Free memory near zero with
    Page Out at 0 %/h is memory that is ALLOCATED, not exhausted: Linux holds
    reclaimable page cache and SAP preallocates its pools at startup.
    Escalating on free memory alone produces false alarms.
    """
    from sap_gui.ocr_extractor import run_ocr
    import re

    goto_tcode(session, "ST06")

    path = capture()
    if not path:
        return {}

    text = run_ocr(path)
    out = {}

    # SAP renders thousands with a dot: "15.643 MB" is 15,643 MB, not 15.643.
    # Reading it as a decimal turns 15 GB of RAM into 15 MB.
    def number(pattern, key, factor=1.0):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return
        raw = match.group(1).replace(".", "").replace(",", ".").strip()
        try:
            out[key] = round(float(raw) * factor, 2)
        except ValueError:
            pass

    number(r"Physical\s+memory\s+([\d.,]+)", "os.memory_total_mb")
    number(r"Free\s+memory\s+([\d.,]+)", "os.memory_free_mb")
    number(r"Free\s+memory\s+incl[^\d]*([\d.,]+)", "os.memory_free_inc_cache_mb")
    number(r"Free\s+swap\s+size\s+([\d.,]+)", "os.swap_free_mb")
    number(r"Actual\s+swap\s+size\s+([\d.,]+)", "os.swap_total_mb")
    number(r"Page\s+Out\s+of\s+RAM\s+([\d.,]+)", "os.page_out_pct_hour")
    number(r"Page\s+In\s+of\s+RAM\s+([\d.,]+)", "os.page_in_pct_hour")
    number(r"Idle\s+([\d.,]+)", "os.cpu_idle_pct")
    number(r"Number\s+of\s+CPUs\s+([\d.,]+)", "os.cpus")

    idle = out.get("os.cpu_idle_pct")
    if idle is not None:
        # 0% CPU is not a reading. A live application server is never at
        # exactly 0, so that value means the OS collector supplied nothing.
        cpu = round(100.0 - idle, 1)
        out["os.cpu_pct"] = cpu if cpu > 0 else None

    total = out.get("os.memory_total_mb")
    free = out.get("os.memory_free_mb")
    if total and free is not None and total > 0:
        out["os.memory_pct"] = round(100.0 - (free / total * 100.0), 1)

    return out


def action_st03n(session, capture):
    """
    Collect structured ST03N workload data through SAP GUI Scripting.

    The ST03N ALV grid is the authoritative source for metrics.
    Screenshot is retained as visual evidence.
    """

    goto_tcode(session, "ST03N")
    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # Navigate to Workload Overview -> Total -> Detailed Analysis
    # ---------------------------------------------------------------

    try:
        tree = session.findById(
            "wnd[0]/shellcont/shell/shellcont[1]/shell"
        )

        tree.selectedNode = "B.999"
        tree.doubleClickNode("B.999")

        wait_until_not_busy(session)

    except Exception as e:
        log.warning(
            f"ST03N node navigation failed: {e}"
        )

    _optional(
        session,
        "wnd[1]/usr/btnBUTTON_2",
        lambda o: o.press(),
        "ST03N confirm popup",
    )

    wait_until_not_busy(session)

    # ---------------------------------------------------------------
    # ST03N workload ALV
    # ---------------------------------------------------------------

    grid = session.findById(
        "wnd[0]/usr/"
        "ssubSUBSCREEN_0:SAPWL_ST03N:1100/"
        "ssubWL_SUBSCREEN_1:SAPWL_ST03N:1110/"
        "tabsG_TABSTRIP/"
        "tabpTA00/"
        "ssubWL_SUBSCREEN_2:SAPWL_ST03N:1130/"
        "cntlALVCONTAINER/"
        "shellcont/shell"
    )

    columns = {
        "task_type": "TASKTYPE",
        "steps": "DIASTEPCNT",
        "avg_response_ms": "MRESPTIME",
        "avg_processing_ms": "MPROCTI",
        "avg_cpu_ms": "MCPUTI",
        "avg_db_ms": "MDBTI",
        "avg_db_processing_ms": "MDBPROCTI",
        "avg_wait_ms": "MWAITTI",
        "avg_roll_in_ms": "MROLLINTI",
        "avg_roll_wait_ms": "MROLWAITI",
        "avg_load_generation_ms": "MLOADGENTI",
        "avg_lock_ms": "MLOCKTI",
        "avg_cpic_rfc_ms": "MCPICTI",
        "avg_network_ms": "FNETMT",
        "avg_gui_ms": "FGUIMT",
        "gui_trips": "FGUICNT",
        "bytes": "BYTES",
        "vmc_call_count": "VMC_CALLCOUNT",
        "vmc_cpu_time": "VMC_CPUTIME",
        "vmc_elapsed_time": "VMC_ELAPTIME",
        "vmc_avg_cpu": "VMC_AVGCPU",
        "vmc_avg_elapsed": "VMC_AVGELAP",
    }

    def cell(row, column):
        try:
            value = grid.GetCellValue(
                row,
                column,
            )

            if value is None:
                return ""

            return str(value).strip()

        except Exception as e:
            log.debug(
                f"ST03N cell read failed: "
                f"row={row}, column={column}, error={e}"
            )
            return ""

    task_types = []

    # ---------------------------------------------------------------
    # Extract every visible ALV row
    # ---------------------------------------------------------------

    for row in range(int(grid.RowCount)):

        item = {
            key: cell(row, technical_name)
            for key, technical_name in columns.items()
        }

        # Ignore completely empty rows.
        if not any(item.values()):
            continue

        task_types.append(item)

    # ---------------------------------------------------------------
    # Screenshot evidence
    # ---------------------------------------------------------------

    screenshot_path = capture()

    result = {
        "task_type_count": len(task_types),
        "task_types": task_types,
    }

    if screenshot_path:
        result["screenshot_captured"] = True

    return result


def action_st22_today(session, capture):
    """
    Collect today's ST22 ABAP runtime errors.

    ST22 has two valid outcomes:

    1. Today contains dumps:
       SAP displays an ALV containing the dump records.

    2. Today contains no dumps:
       SAP remains on the ST22 selection screen and no ALV may exist.

    Both outcomes are successful monitoring results.

    Navigation is intentionally minimal:
        ST22 -> TODAY -> capture -> inspect ALV if available
    """

    import time

    goto_tcode(session, "ST22")
    wait_until_not_busy(session)

    log.info("ST22: opened ST22. Selecting TODAY only.")

    # ---------------------------------------------------------------
    # Select TODAY
    # ---------------------------------------------------------------

    today_button = session.findById(
        "wnd[0]/usr/btnTODAY"
    )

    log.info("ST22: TODAY button found. Pressing it.")

    today_button.press()

    wait_until_not_busy(session)

    # Give SAP GUI time to update the screen.
    time.sleep(0.8)

    log.info("ST22: TODAY selection completed.")

    # ---------------------------------------------------------------
    # Capture screenshot immediately.
    #
    # IMPORTANT:
    # Do this BEFORE looking for the ALV.
    #
    # When there are zero dumps, SAP may not create the ALV control.
    # The screenshot is still valid evidence.
    # ---------------------------------------------------------------

    screenshot_path = None

    try:
        screenshot_path = capture("today")
        log.info(
            "ST22: TODAY screenshot captured: %s",
            screenshot_path,
        )
    except Exception as e:
        log.warning(
            "ST22: TODAY screenshot capture failed: %s: %s",
            type(e).__name__,
            e,
        )

    # ---------------------------------------------------------------
    # Check status bar.
    #
    # With zero dumps SAP commonly reports something similar to:
    #
    #   No short dumps match the selection criteria
    #
    # Do not depend exclusively on the exact wording because it can
    # vary between SAP versions/languages.
    # ---------------------------------------------------------------

    status_text = ""

    try:
        status_bar = session.findById("wnd[0]/sbar")
        status_text = str(
            getattr(status_bar, "Text", "") or ""
        ).strip()

        if status_text:
            log.info(
                "ST22: status bar = %r",
                status_text,
            )

    except Exception as e:
        log.debug(
            "ST22: could not read status bar: %s: %s",
            type(e).__name__,
            e,
        )

    # ---------------------------------------------------------------
    # Try to find the ALV.
    #
    # ALV exists when one or more dumps are displayed.
    # It may NOT exist when today's count is zero.
    # ---------------------------------------------------------------

    alv_id = (
        "wnd[0]/usr/"
        "cntlRSSHOWRABAX_ALV_100/"
        "shellcont/shell"
    )

    grid = None

    try:
        grid = session.findById(alv_id)
        log.info("ST22: TODAY ALV found.")

    except Exception as e:
        log.info(
            "ST22: TODAY ALV is not available. "
            "This can be normal when there are zero dumps. "
            "status=%r error=%s",
            status_text,
            type(e).__name__,
        )

    # ---------------------------------------------------------------
    # ZERO-DUMP CASE
    # ---------------------------------------------------------------

    if grid is None:

        log.info(
            "ST22: No TODAY ALV detected. "
            "Treating TODAY as zero dumps."
        )

        return {
            "dump_count": 0,
            "dumps": [],
            "unique_users": [],
            "unique_programs": [],
            "unique_runtime_errors": [],
            "screenshot_captured": bool(screenshot_path),
            "status_message": status_text,
        }

    # ---------------------------------------------------------------
    # ALV EXISTS -> read rows
    # ---------------------------------------------------------------

    columns = [
        "DATUM",
        "UZEIT",
        "AHOST",
        "UNAME",
        "MANDT",
        "XHOLD",
        "ERRORID",
        "REXCEPTION",
        "GPROGRAM",
        "MODNO",
        "TID",
    ]

    try:
        row_count = int(grid.RowCount)
    except Exception as e:
        log.warning(
            "ST22: Could not read ALV RowCount: %s: %s",
            type(e).__name__,
            e,
        )
        row_count = 0

    log.info(
        "ST22: TODAY ALV contains %d row(s).",
        row_count,
    )

    dumps = []

    # ---------------------------------------------------------------
    # Extract every dump
    # ---------------------------------------------------------------

    for row in range(row_count):

        record = {}

        for column in columns:

            try:
                value = grid.GetCellValue(
                    row,
                    column,
                )

                if value is None:
                    value = ""

                record[column] = str(value).strip()

            except Exception as e:

                log.warning(
                    "ST22: Could not read row=%d column=%s: %s",
                    row,
                    column,
                    type(e).__name__,
                )

                record[column] = ""

        dump = {
            "date": record.get("DATUM", ""),
            "time": record.get("UZEIT", ""),
            "host": record.get("AHOST", ""),
            "user": record.get("UNAME", ""),
            "client": record.get("MANDT", ""),
            "hold_status": record.get("XHOLD", ""),
            "runtime_error": record.get("ERRORID", ""),
            "exception": record.get("REXCEPTION", ""),
            "program": record.get("GPROGRAM", ""),
            "module": record.get("MODNO", ""),
            "transaction_id": record.get("TID", ""),
        }

        # Ignore completely empty rows.
        if not any(
            str(value).strip()
            for value in dump.values()
        ):
            continue

        dumps.append(dump)

        log.info(
            "ST22: dump[%d] date=%s time=%s "
            "user=%s error=%s program=%s",
            len(dumps),
            dump["date"],
            dump["time"],
            dump["user"],
            dump["runtime_error"],
            dump["program"],
        )

    # ---------------------------------------------------------------
    # Aggregate information
    # ---------------------------------------------------------------

    users = sorted(
        {
            dump["user"]
            for dump in dumps
            if dump.get("user")
        }
    )

    programs = sorted(
        {
            dump["program"]
            for dump in dumps
            if dump.get("program")
        }
    )

    runtime_errors = sorted(
        {
            dump["runtime_error"]
            for dump in dumps
            if dump.get("runtime_error")
        }
    )

    log.info(
        "ST22: TODAY collection complete: "
        "dump_count=%d users=%d programs=%d runtime_errors=%d",
        len(dumps),
        len(users),
        len(programs),
        len(runtime_errors),
    )

    return {
        "dump_count": len(dumps),
        "dumps": dumps,
        "unique_users": users,
        "unique_programs": programs,
        "unique_runtime_errors": runtime_errors,
        "screenshot_captured": bool(screenshot_path),
        "status_message": status_text,
    }


ACTIONS = {
    "st06": action_st06,
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
    "sm50": action_sm50,
    "sm66": action_sm66,
    "smlg": action_smlg,
    "smq1": action_smq1,
    "smq2": action_smq2,
    "sost": action_sost,
    "sp01": action_sp01,
    "st03n": action_st03n,
    "st22_today": action_st22_today,
}
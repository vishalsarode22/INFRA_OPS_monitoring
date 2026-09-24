"""
InfraBeatOps Excel Report Writer

Purpose
-------
Populate the InfraBeatOps SAP BASIS monitoring Excel template using
structured SAP GUI results.

Important
---------
- Excel receives structured monitoring values.
- PDF continues to contain screenshots/evidence.
- Evidence IDs are NOT written into the monitoring data.
- OCR is used only as a fallback when structured data is unavailable.
- SMLG response time is evaluated deterministically.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from core.models import MetricResult


NO_DATA_TEXT = "See attached screenshot for details."


# ---------------------------------------------------------------------------
# Template rows
# ---------------------------------------------------------------------------
#
# This is the actual InfraBeatOps template layout.
#
# Row 3 contains:
#   Date | Time | System | SID | Client | Environment |
#   T-Code | Check | Actual Result | Status | Observation
#
# Data starts at row 4.
#
# Must stay aligned with config/templates/InfraBeatOps_Simple_Monitoring_
# Sheet.xlsx row for row. It previously omitted SM21, which had two
# consequences: SM21 never reached the sheet at all even though it is
# collected every cycle and appears in the PDF, and every row from 11
# downwards was written one row above its template label -- so the last
# template row (ST22) was never written over and survived into the output
# as a blank duplicate of the ST22 row above it.
TCODE_ROW_MAP = {
    4: "AL08",
    5: "DB01",
    6: "DB02",
    7: "DB12",
    8: "SCOT",
    9: "SM12",
    10: "SM13",
    11: "SM21",
    12: "SM37",
    13: "SM37_CANCELLED",
    14: "SM51",
    15: "SM58",
    16: "SM66",
    17: "SMLG",
    18: "SMQ1",
    19: "SMQ2",
    20: "SOST",
    21: "SP01",
    22: "ST22",
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    """Convert a value into clean display text."""
    if value is None:
        return ""

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))

    return str(value).strip()


def _number(value: Any):
    """Safely convert a value to float/int where possible."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return value

    text = str(value).strip().replace(",", "")

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        number = float(match.group(0))

        if number.is_integer():
            return int(number)

        return number

    except (ValueError, TypeError):
        return None


def _format_number(value: Any) -> str:
    """Display numeric values without unnecessary .0."""
    number = _number(value)

    if number is None:
        return _clean(value)

    if isinstance(number, float) and number.is_integer():
        return f"{int(number):,}"

    if isinstance(number, int):
        return f"{number:,}"

    return f"{number:,}" if isinstance(number, float) else str(number)


def _get_data(metric: MetricResult) -> dict:
    """Return metric extra_data safely."""
    data = getattr(metric, "extra_data", None)

    if isinstance(data, dict):
        return data

    return {}


# A successful backup older than this is overdue (daily backups on PS4 finish
# around 14:45; 26 h leaves room for a run that starts a little late).
DB12_MAX_BACKUP_AGE_HOURS = 26

# SM21 errors after routine and dump messages are set aside: 1-4 is WARNING,
# from this many CRITICAL. One lost RFC conversation (R49 CONV_ID_NOT_FOUND on
# PS4, 22.09.2026) had made SM21 CRITICAL on its own.
SM21_ERRORS_CRITICAL = 5

# Checks whose figure is also published as a metric graded by
# config/thresholds.yaml. The row takes the worse of its own rule and the
# threshold, so one number cannot be WARNING in the sheet and CRITICAL in
# the findings (PS4: 680 locks and 6 dumps were both).
_THRESHOLD_GRADED = {
    # tcode: (evidence key, metric name in thresholds.yaml -- as core/tcode_metrics.py names it)
    "SM12": (("lock_count",), "sap.sm12.lock_count"),
    "ST22": (("dump_count",), "sap.st22.dump_count"),
    # failed = in error + Initial past the stale limit; older payloads only
    # carry the raw record count.
    "SM13": (("failed_update_count", "update_count"), "sap.sm13.failed_updates"),
}


def _threshold_limits(tcode: str):
    """(warning, critical) from thresholds.yaml for this check's figure."""
    spec = _THRESHOLD_GRADED.get(tcode)
    if not spec:
        return None
    try:
        from core.config_loader import get_thresholds

        table = get_thresholds() or {}
        table = table.get("thresholds", table)
        limits = table.get(spec[1])
    except Exception:
        return None
    if not isinstance(limits, dict):
        return None
    return _number(limits.get("warning")), _number(limits.get("critical"))


def _threshold_status(tcode: str, data: dict):
    limits = _threshold_limits(tcode)
    keys = (_THRESHOLD_GRADED.get(tcode) or ((),))[0]
    value = next((v for v in (_number(data.get(k)) for k in keys) if v is not None), None)
    if not limits or value is None:
        return None
    warning, critical = limits
    # Exclusive (value > limit), the same boundary the metric engine uses:
    # 5 dumps against critical=5 graded WARNING as a metric and CRITICAL in
    # the sheet on PS4, 23.09.2026.
    if critical is not None and value > critical:
        return "CRITICAL"
    if warning is not None and value > warning:
        return "WARNING"
    return "OK"


def _backup_age_hours(data: dict):
    latest = data.get("latest_backup") or {}
    try:
        ended = datetime.strptime(_clean(latest.get("end_time"))[:19], "%d.%m.%Y %H:%M:%S")
        taken = datetime.strptime(_clean(data.get("finished_at"))[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (taken - ended).total_seconds() / 3600


def _short_program(name) -> str:
    """CL_ABAP_TYPEDESCR=============CP -> CL_ABAP_TYPEDESCR"""
    return re.sub(r"=+\w*$", "", _clean(name))


def _not_measured(reason: str) -> str:
    return f"Not measured: {reason}. See the screenshot for what was displayed."


def _sm21_zero_contradicted(data: dict) -> bool:
    """
    An SM21 read that found no rows while OCR of the same screen counted
    syslog rows. PS4 reported "0 entries, OK" beside 30+ visible messages;
    that must read as unverified, not as a clean log.
    """
    return (_number(data.get("log_count")) == 0
            and (_number(data.get("syslog_rows_count")) or 0) > 0)


def _sm37_zero_contradicted(data: dict) -> bool:
    """SM37 counted 0 active jobs from rows whose status column read blank."""
    jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
    return (_number(data.get("active_job_count")) == 0
            and (_number(data.get("records_passed")) or 0) > 0
            and bool(jobs)
            and not any(str(j.get("status") or "").strip() for j in jobs))


def _usage(item) -> dict:
    """Re-derive a DB02 usage reading from its raw text (locale-safe)."""
    from utils.sap_numbers import parse_usage_ratio

    if not isinstance(item, dict):
        return {}
    raw = _clean(item.get("raw"))
    parsed = parse_usage_ratio(raw) if raw else {}
    if parsed.get("usage_percent") is not None:
        return parsed
    return item


def _fmt2(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return _clean(value)


_DB02_ITEMS = (
    ("Memory MDC", "memory", "mdc"),
    ("Memory Tenant", "memory", "tenant"),
    ("Storage Data", "storage", "data"),
    ("Storage Log", "storage", "log"),
    ("Storage Trace", "storage", "trace"),
)


def _capture_time(metric: MetricResult):
    """(dd.mm.yyyy, HH:MM:SS) of this check's own capture, if recorded."""
    stamp = _clean(_get_data(metric).get("finished_at"))
    try:
        when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
        return when.strftime("%d.%m.%Y"), when.strftime("%H:%M:%S")
    except ValueError:
        return None


def _system_config(name: str) -> dict:
    try:
        from core.config_loader import get_systems

        for system in get_systems() or []:
            if _clean(system.get("name")).upper() == _clean(name).upper():
                return system
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Status evaluation
# ---------------------------------------------------------------------------

def _status_from_smlg(data: dict) -> str:
    """
    SMLG response-time monitoring.

        <= 1500 ms   -> OK
        >  1500 ms   -> WARNING
        >  2000 ms   -> CRITICAL
        >  2500 ms   -> CRITICAL (flashing on the dashboard)

    Graded by sap_gui/smlg_analyzer.py rather than by a second copy of the
    numbers here, so the sheet, the PDF check row and the dashboard chip
    cannot drift apart. The old local thresholds had already drifted: they
    called 1500 ms CRITICAL while the metric called it WARNING.

    Per-instance readings are preferred, but the scalar the OCR path writes
    is accepted too. Only accepting the `instances` list is why a captured
    "Response Time Ms: 63" still came out as UNKNOWN -- the figure reached
    the sheet and was graded nowhere.
    """
    from sap_gui.smlg_analyzer import classify_response_time

    response_times = []

    for instance in (data.get("instances") or []):
        value = _number(instance.get("response_time_ms"))
        if value is not None:
            response_times.append(float(value))

    if not response_times:
        # parse_response_ms handles SMLG's thousands separator; _number
        # would turn the "1.265" on screen into 1.265 ms.
        from sap_gui.smlg_analyzer import parse_response_ms

        for key in ("response_time_ms", "avg_response_time_ms"):
            value = parse_response_ms(data.get(key))
            if value is not None:
                response_times.append(float(value))
                break

    if not response_times:
        return "UNKNOWN"

    grade = classify_response_time(max(response_times))

    # Status.NORMAL is spelled "OK" in the sheet's vocabulary.
    return "OK" if grade["status"].value == "NORMAL" else grade["status"].value


def _rule_status_for_metric(metric: MetricResult) -> str:
    """
    Determine deterministic Excel monitoring status.

    Priority:
    1. T-code-specific structured evidence
    2. Existing MetricResult status
    3. UNKNOWN
    """

    tcode = _clean(getattr(metric, "tcode", "")).upper()
    data = _get_data(metric)

    # ---------------------------------------------------------------
    # A check whose extraction failed was not measured, whatever else is
    # in its data. Without this, a T-code with no specific rule fell back
    # to the metric's own status -- and SM13 was graded OK beside the words
    # "SM13 structured extraction failed" because its failure path had
    # reported zero pending updates.
    # ---------------------------------------------------------------
    if data.get("extraction_failed"):
        return "UNKNOWN"

    # ---------------------------------------------------------------
    # SMLG - response time
    # ---------------------------------------------------------------
    if tcode == "SMLG":
        return _status_from_smlg(data)

    # ---------------------------------------------------------------
    # AL08 - Logged-On Users
    # ---------------------------------------------------------------
    if tcode == "AL08":
        extraction_method = _clean(
            data.get("extraction_method")
        )

        # Successful native ALV collection is a successful check.
        if extraction_method == "sap_gui_alv":
            return "OK"

        # Preserve an explicitly supplied status if one exists.
        explicit_status = _clean(
            data.get("status")
        ).upper()

        if explicit_status in {
            "OK",
            "WARNING",
            "CRITICAL",
            "UNKNOWN",
        }:
            return explicit_status

        # AL08 answers one question: how many users are logged on right now.
        # Any route that produced that count -- native ALV or the OCR read of
        # the footer -- is a successful check. Only the ALV path counted
        # before, so a screen reporting 16 logged-on users was still filed as
        # NOT COLLECTED.
        for key in ("user_logons", "total_active_sessions",
                    "back_end_sessions", "unique_users"):
            if _number(data.get(key)) is not None:
                return "OK"

    # ---------------------------------------------------------------
    # DB01 - Database Locks / Deadlocks
    # ---------------------------------------------------------------
    if tcode == "DB01":
        deadlocks = _number(data.get("deadlock_count"))
        locks = _number(data.get("lock_count"))

        if deadlocks is not None and deadlocks > 0:
            return "CRITICAL"

        if locks is not None and locks > 0:
            return "WARNING"

        if deadlocks is not None or locks is not None:
            return "OK"

    # ---------------------------------------------------------------
    # DB02 - Database Space / Storage
    # ---------------------------------------------------------------
    if tcode == "DB02":
        # DB02 monitoring is intentionally limited to the SAP Overview
        # memory/storage values. Do not derive health from tablespace counts.
        explicit_status = _clean(data.get("status")).upper()
        if explicit_status in {"OK", "WARNING", "CRITICAL", "UNKNOWN"}:
            return explicit_status

        if data.get("memory") or data.get("storage"):
            return "OK"

    # ---------------------------------------------------------------
    # DB12 - Database Backup Status
    # ---------------------------------------------------------------
    if tcode == "DB12":
        latest = data.get("latest_backup") or {}
        latest_status = _clean(latest.get("status")).lower()
        if latest_status in {"failed", "error", "cancelled", "canceled"}:
            return "CRITICAL"
        if latest_status == "successful":
            age = _backup_age_hours(data)
            if age is not None and age > DB12_MAX_BACKUP_AGE_HOURS:
                return "WARNING"
            return "OK"

        # Backward-compatible fallback for older payloads.
        failed = _number(data.get("failed_backups"))
        successful = _number(data.get("successful_backups"))
        if failed is not None and failed > 0:
            return "CRITICAL"
        if successful is not None and successful > 0:
            return "OK"
        if _number(data.get("backup_count")) == 0:
            return "UNKNOWN"

    # ---------------------------------------------------------------
    # SCOT - SAPconnect / SMTP
    # ---------------------------------------------------------------
    if tcode == "SCOT":
        # A listed mail port -- any number -- is the health signal for this
        # check, so it decides first. The rules below it read smtp_status,
        # active_nodes and node_count, which the current SCOT action never
        # produces; they are kept for results from older collectors.
        if _number(data.get("mail_port")) is not None:
            return "OK"

        if "mail_port_raw" in data and not data.get("extraction_failed"):
            # The screen was read and no port number was listed.
            return "WARNING"

        smtp_status = _clean(data.get("smtp_status")).lower()
        active_nodes = _number(data.get("active_nodes"))
        node_count = _number(data.get("node_count"))

        if any(
            value in smtp_status
            for value in (
                "inactive",
                "disabled",
                "not active",
                "error",
                "failed",
            )
        ):
            return "CRITICAL"

        if any(
            value in smtp_status
            for value in (
                "warning",
                "attention",
            )
        ):
            return "WARNING"

        if (
            "active" in smtp_status
            or "enabled" in smtp_status
            or "working" in smtp_status
        ):
            return "OK"

        if active_nodes is not None and active_nodes > 0:
            return "OK"

        if node_count is not None and node_count > 0:
            return "WARNING"

    # ---------------------------------------------------------------
    # SM21 - System Log
    # ---------------------------------------------------------------
    if tcode == "SM21":
        if _sm21_zero_contradicted(data):
            return "UNKNOWN"

        errors = _number(data.get("error_count"))
        warnings = _number(data.get("warning_count"))
        logs = _number(data.get("log_count"))

        if errors is not None and errors >= SM21_ERRORS_CRITICAL:
            return "CRITICAL"

        if errors is not None and errors > 0:
            return "WARNING"

        if warnings is not None and warnings > 0:
            return "WARNING"

        if logs is not None:
            return "OK"

    # ---------------------------------------------------------------
    # SM37 / SM37_CANCELLED / SM51
    # ---------------------------------------------------------------
    # These actions return an authoritative status alongside their
    # structured counts. Do not interpret a non-zero count as unhealthy
    # by itself (e.g. active jobs and active application servers are normal).
    if tcode in {"SM37", "SM37_CANCELLED", "SM51"}:
        if tcode == "SM37" and _sm37_zero_contradicted(data):
            return "UNKNOWN"
        explicit_status = _clean(data.get("status")).upper()
        if explicit_status in {"OK", "WARNING", "CRITICAL", "UNKNOWN"}:
            return explicit_status

    # ---------------------------------------------------------------
    # SM58 - tRFC Information
    # ---------------------------------------------------------------
    if tcode == "SM58":
        # action_sm58 explicitly determines the status from the
        # Information section, including failed_entries.
        explicit_status = _clean(
            data.get("trfc_status")
        ).upper()

        if explicit_status == "HEALTHY":
            return "OK"

        if explicit_status in {
            "OK",
            "WARNING",
            "CRITICAL",
            "UNKNOWN",
            "NO_DATA",
        }:
            if explicit_status == "NO_DATA":
                return "UNKNOWN"
            return explicit_status

        # Backward-compatible fallback.
        failed = _number(data.get("failed_entries"))
        count = _number(data.get("trfc_count"))

        if failed is not None and failed > 0:
            return "CRITICAL"
        if count is not None:
            return "WARNING" if count > 10 else "OK"

        if count is not None:
            return "OK" if count == 0 else "WARNING"

    # ---------------------------------------------------------------
    # SM12
    # ---------------------------------------------------------------
    if tcode == "SM12":
        count = _number(data.get("lock_count"))

        if count is not None:
            # Follow thresholds.yaml (500/2000 since 22.09.2026). The fixed
            # "over 50" rule below is the fallback when no limit is
            # configured; it used to flag every productive system.
            graded = _threshold_status("SM12", data)
            if graded:
                return graded
            return "WARNING" if count > 50 else "OK"

    # ---------------------------------------------------------------
    # SM13
    # ---------------------------------------------------------------
    if tcode == "SM13":
        if _number(data.get("error_update_count")):
            return "CRITICAL"

        failed = _number(data.get("failed_update_count"))
        if failed is not None:
            return "WARNING" if failed else "OK"

        count = _number(data.get("update_count"))

        if count is not None:
            return "OK" if count == 0 else "WARNING"

    # ---------------------------------------------------------------
    # SM66
    # ---------------------------------------------------------------
    if tcode == "SM66":
        failed = _number(data.get("failed_processes"))

        if failed is not None and failed > 0:
            return "CRITICAL"

        # The two states worth acting on. A screen with neither is healthy,
        # not unknown -- this check fell through to UNKNOWN whenever
        # failed_processes was absent, which is every OCR-fallback capture.
        for key in ("priv_mode_processes",):
            value = _number(data.get(key))
            if value is not None and value > 0:
                return "WARNING"

        for key in ("active_processes", "running_processes", "visible_process_rows",
                    "process_count", "failed_processes"):
            if _number(data.get(key)) is not None:
                return "OK"


    # ---------------------------------------------------------------
    # SMQ1 / SMQ2
    # ---------------------------------------------------------------
    # ---------------------------------------------------------------
    # SM51 - Application servers
    #
    # A system whose instances are all active is healthy. There was no rule
    # here at all, so a perfectly good "1 AS instance(s) started" reading
    # fell through to UNKNOWN and the report printed NOT COLLECTED beside
    # the figure it had just collected.
    # ---------------------------------------------------------------
    if tcode == "SM51":
        explicit_status = _clean(data.get("status")).upper()
        if explicit_status in {"OK", "WARNING", "CRITICAL", "UNKNOWN"}:
            return explicit_status

        started = _number(data.get("instances_started"))
        if started is None:
            started = _number(data.get("instance_count"))

        active = _number(data.get("active_instances"))

        if started is not None:
            # The OCR fallback can count instances but cannot read their
            # state, so a started instance counts as active unless the
            # structured read says otherwise.
            if active is None:
                return "OK" if started > 0 else "CRITICAL"
            if started > 0 and active >= started:
                return "OK"
            if active > 0:
                return "WARNING"
            return "CRITICAL"

    if tcode in ("SMQ1", "SMQ2"):
        entries = _number(data.get("entries_displayed"))

        if entries is not None:
            return "OK" if entries == 0 else "WARNING"

    # ---------------------------------------------------------------
    # SP01 - Spool Requests
    # ---------------------------------------------------------------
    if tcode == "SP01":
        errors = _number(data.get("spool_errors"))
        requests = _number(data.get("spool_requests"))

        if errors is not None and errors > 0:
            return "WARNING"

        if requests is not None:
            return "OK"

        return "UNKNOWN"

    # ---------------------------------------------------------------
    # ST22
    # ---------------------------------------------------------------
    if tcode == "ST22":
        dumps = _number(data.get("dump_count"))

        if dumps is not None:
            return "OK" if dumps == 0 else "WARNING"

    # ---------------------------------------------------------------
    # SOST
    # ---------------------------------------------------------------
    if tcode == "SOST":
        errors = _number(data.get("errors"))
        waiting = _number(data.get("waiting"))
        send_requests = _number(data.get("send_requests"))
        sent = _number(data.get("sent"))

        if errors is not None and errors > 0:
            return "WARNING"

        if waiting is not None and waiting > 50:
            return "WARNING"

        # A backlog with nothing going out is the finding that matters, and
        # it was being reported as OK: the old rule looked only at errors and
        # waiting, so a screen reading "669 Send, 0 Sent, 0 Errors" passed
        # clean. Nothing leaving the system usually means no active SMTP node
        # in SCOT, which is exactly what this system's SCOT screen shows.
        if send_requests is not None and send_requests > 0 and sent == 0:
            return "WARNING"

        if any(v is not None for v in (errors, waiting, send_requests, sent)):
            return "OK"

    # ---------------------------------------------------------------
    # Existing MetricResult status
    # ---------------------------------------------------------------
    status = getattr(metric, "status", None)

    if status is not None:
        status_value = getattr(status, "value", status)

        if status_value:
            normalized = str(status_value).upper()

            if normalized in {
                "OK",
                "WARNING",
                "CRITICAL",
                "UNKNOWN",
            }:
                return normalized

    return "UNKNOWN"

_STATUS_RANK = {"UNKNOWN": 0, "OK": 0, "WARNING": 1, "CRITICAL": 2}


def _status_for_metric(metric: MetricResult) -> str:
    """The check's rule, raised to the thresholds.yaml grade where one applies."""
    status = _rule_status_for_metric(metric)
    if status == "UNKNOWN":
        return status
    tcode = _clean(getattr(metric, "tcode", "")).upper()
    graded = _threshold_status(tcode, _get_data(metric))
    if graded and _STATUS_RANK[graded] > _STATUS_RANK.get(status, 0):
        return graded
    return status


# ---------------------------------------------------------------------------
# Actual Result
# ---------------------------------------------------------------------------

def _build_actual_result(metric: MetricResult) -> str:
    text = _build_actual_result_base(metric)
    data = _get_data(metric)
    if (_clean(getattr(metric, "tcode", "")).upper() == "SM21" and text
            and not text.startswith(("Not measured", "Not verified"))):
        routine = _number(data.get("routine_count"))
        echoes = _number(data.get("dump_echo_count"))
        if routine:
            text += f"; routine: {_format_number(routine)}"
        if echoes:
            text += f"; dump messages: {_format_number(echoes)} (see ST22)"
    return text


def _build_actual_result_base(metric: MetricResult) -> str:
    """
    Build the Excel Actual Result from structured SAP GUI evidence.
    """

    tcode = _clean(getattr(metric, "tcode", "")).upper()
    data = _get_data(metric)

    if not data:
        return NO_DATA_TEXT

    # Say that the check was not measured, and why -- rather than printing
    # a count the tool never read.
    if data.get("extraction_failed"):
        reason = _clean(data.get("error"))
        if len(reason) > 140:
            reason = reason[:137] + "..."
        return (
            "Not measured: the screen was opened but its values could not "
            "be read" + (f" ({reason})" if reason else "") + ". "
            "See the screenshot for what was displayed."
        )

    if tcode == "SM21" and _sm21_zero_contradicted(data):
        return ("Not verified: the log grid read as empty while the screen "
                f"lists about {_format_number(data.get('syslog_rows_count'))} "
                "entries. See the screenshot.")

    if tcode == "SM37" and _sm37_zero_contradicted(data):
        return (f"Not verified: {_format_number(data.get('records_passed'))} jobs "
                "listed (Ready/Active), but the status column could not be read. "
                "See the screenshot.")

    # ---------------------------------------------------------------
    # AL08 - Logged-On Users
    # ---------------------------------------------------------------
    if tcode == "AL08":
        # Current AL08 action returns these structured counters directly.
        total_sessions = data.get("total_active_sessions")
        unique_users = data.get("unique_users")
        gui_sessions = data.get("gui_sessions")
        rfc_sessions = data.get("rfc_sessions")
        background_sessions = data.get("background_sessions")

        parts = []

        if total_sessions is not None:
            parts.append(
                f"Active Sessions: {_format_number(total_sessions)}"
            )

        if unique_users is not None:
            parts.append(
                f"Unique Users: {_format_number(unique_users)}"
            )

        if gui_sessions is not None:
            parts.append(
                f"GUI: {_format_number(gui_sessions)}"
            )

        if rfc_sessions is not None:
            parts.append(
                f"RFC: {_format_number(rfc_sessions)}"
            )

        if background_sessions is not None:
            parts.append(
                f"Background: {_format_number(background_sessions)}"
            )

        if parts:
            return "; ".join(parts)

        # The OCR read of AL08's footer ("N user sessions with M ABAP
        # sessions") writes these two keys. Only the native-ALV key names
        # were handled above, so on this system the whole extra_data dict
        # was dumped into the cell instead -- OCR line counts, timestamps
        # and all -- burying the one number AL08 exists to report.
        user_logons = data.get("user_logons")
        back_end_sessions = data.get("back_end_sessions")

        parts = []

        if user_logons is not None:
            parts.append(
                f"{_format_number(user_logons)} user sessions"
            )

        if back_end_sessions is not None:
            parts.append(
                f"{_format_number(back_end_sessions)} ABAP sessions"
            )

        if parts:
            return "; ".join(parts)

        # Backward compatibility with older AL08 result schema.
        summary = data.get("session_summary")
        if summary:
            return _clean(summary)

        users = data.get("user_count")
        sessions = data.get("session_count")

        parts = []

        if users is not None:
            parts.append(
                f"{_format_number(users)} user logons"
            )

        if sessions is not None:
            parts.append(
                f"{_format_number(sessions)} back-end sessions"
            )

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # DB01
    # ---------------------------------------------------------------
    if tcode == "DB01":
        locks = _number(data.get("lock_count"))
        deadlocks = _number(data.get("deadlock_count"))

        parts = []

        if locks is not None:
            parts.append(
                f"Locks: {_format_number(locks)}"
            )

        if deadlocks is not None:
            parts.append(
                f"Deadlocks: {_format_number(deadlocks)}"
            )

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # DB02
    # ---------------------------------------------------------------
    if tcode == "DB02":
        parts = []
        for label, group, key in _DB02_ITEMS:
            item = _usage((data.get(group) or {}).get(key))
            pct = item.get("usage_percent")
            if item.get("used") is None or item.get("limit") is None:
                continue
            text = (f"{label}: {_fmt2(item['used'])} {item.get('unit', '')} of "
                    f"{_fmt2(item['limit'])} {item.get('limit_unit') or item.get('unit', '')}")
            if pct is not None:
                text += f" ({float(pct):.1f}%)"
            parts.append(text)

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # DB12
    # ---------------------------------------------------------------
    if tcode == "DB12":
        latest = data.get("latest_backup") or {}
        end_time = _clean(latest.get("end_time")) or _clean(data.get("last_backup_end_time"))
        if end_time:
            state = _clean(latest.get("status")).lower()
            text = (f"Last successful backup: {end_time}" if state in ("", "successful")
                    else f"Last backup: {end_time} ({state})")
            running = data.get("running_backup") or {}
            if _clean(running.get("start_time")):
                text += f"; backup running since {_clean(running.get('start_time'))}"
            return text

    # ---------------------------------------------------------------
    # SCOT
    # ---------------------------------------------------------------
    if tcode == "SCOT":
        # Show the port that was read. This branch only knew the older
        # smtp_status/node_count keys, so a successfully captured port fell
        # through to the raw extra_data dump and never appeared on its own.
        port = data.get("mail_port")
        if _number(port) is not None:
            return f"SMTP Mail Port: {int(_number(port))}"

        raw_port = _clean(data.get("mail_port_raw"))
        if "mail_port_raw" in data and not data.get("extraction_failed"):
            if _number(data.get("smtp_nodes_configured")) == 0:
                return ("No SMTP node configured: the SMTP Nodes list in SCOT "
                        "is empty, so outbound mail cannot be sent")
            return (f"SMTP Mail Port field shows {raw_port!r}, not a port number"
                    if raw_port else "No SMTP Mail Port listed")

        smtp_status = data.get("smtp_status")
        nodes = _number(data.get("node_count"))
        active = _number(data.get("active_nodes"))

        parts = []

        if smtp_status:
            parts.append(
                f"SMTP Status: {_clean(smtp_status)}"
            )

        if nodes is not None:
            parts.append(
                f"Nodes: {_format_number(nodes)}"
            )

        if active is not None:
            parts.append(
                f"Active Nodes: {_format_number(active)}"
            )

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # SM12
    # ---------------------------------------------------------------
    if tcode == "SM12":
        count = data.get("lock_count")

        if count is not None:
            return (
                f"{_format_number(count)} lock entries"
            )

    # ---------------------------------------------------------------
    # SM13
    # ---------------------------------------------------------------
    if tcode == "SM13":
        summary = data.get("update_summary")

        if summary:
            return _clean(summary)

        count = data.get("update_count")

        if count is not None:
            return (
                f"{_format_number(count)} update records"
            )

    # ---------------------------------------------------------------
    # SM21
    # ---------------------------------------------------------------
    if tcode == "SM21":
        logs = _number(data.get("log_count"))
        errors = _number(data.get("error_count"))
        warnings = _number(data.get("warning_count"))

        parts = []

        if logs is not None:
            parts.append(
                f"Log Entries: {_format_number(logs)}"
            )

        if errors is not None:
            parts.append(
                f"Errors: {_format_number(errors)}"
            )

        if warnings is not None:
            parts.append(
                f"Warnings: {_format_number(warnings)}"
            )

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # SM37 active
    # ---------------------------------------------------------------
    if tcode == "SM37":
        active = _number(data.get("active_job_count"))
        ready = _number(data.get("ready_job_count"))
        total = _number(data.get("job_count"))
        if active is None and isinstance(data.get("active_jobs"), (int, float, str)):
            active = _number(data.get("active_jobs"))

        if active is not None:
            text = f"{_format_number(active)} active background jobs"
            if ready:
                text += f"; {_format_number(ready)} ready"
            longest = data.get("longest_running_job") or {}
            if longest.get("job_name") and _number(longest.get("duration_seconds")):
                text += (f"; longest running {longest['job_name']} "
                         f"({_format_number(longest['duration_seconds'])} s)")
            return text

        if total is not None:
            return (f"{_format_number(total)} jobs in Ready/Active status "
                    "(per-job status not readable)")

    if tcode == "SM37_CANCELLED":
        count = _number(data.get("cancelled_job_count"))
        if count is None:
            count = _number(data.get("records_passed"))
        since = _clean((data.get("date_filter") or {}).get("from_date"))

        if count is not None:
            text = f"{_format_number(count)} cancelled background jobs"
            if since:
                text += f" since {since}"
            names = data.get("cancelled_job_names") or []
            if count and names:
                text += ": " + ", ".join(names[:5])
            return text

        return _not_measured("the cancelled-job count could not be read")

    # ---------------------------------------------------------------
    # SM51
    # ---------------------------------------------------------------
    if tcode == "SM51":
        count = data.get("instances_started")

        if count is None:
            count = data.get("instance_count")

        if count is not None:
            return (
                f"{_format_number(count)} active application server"
                f"{'s' if _number(count) != 1 else ''}"
            )

    # ---------------------------------------------------------------
    # SM58 - Information section only
    # ---------------------------------------------------------------
    if tcode == "SM58":
        information = data.get("information") or {}

        entries_displayed = information.get(
            "entries_displayed",
            data.get("trfc_count"),
        )
        failed_entries = information.get(
            "failed_entries",
            data.get("failed_entries"),
        )
        entries_in_execution = information.get(
            "entries_in_execution",
            data.get("entries_in_execution"),
        )

        parts = []

        if entries_displayed is not None:
            parts.append(
                f"Entries Displayed: {_format_number(entries_displayed)}"
            )

        if failed_entries is not None:
            parts.append(
                f"Failed Entries: {_format_number(failed_entries)}"
            )

        if entries_in_execution is not None:
            parts.append(
                f"Entries in Execution: {_format_number(entries_in_execution)}"
            )

        if parts:
            return "; ".join(parts)

        status = data.get("trfc_status")

        if status:
            return _clean(status)


    # ---------------------------------------------------------------
    # SMLG
    # ---------------------------------------------------------------
    if tcode == "SMLG":
        from sap_gui.smlg_analyzer import parse_response_ms

        readings = []
        for instance in data.get("instances") or []:
            response = parse_response_ms(instance.get("response_time_ms"))
            if response is not None:
                readings.append((_clean(instance.get("instance")), response))

        if readings:
            readings.sort(key=lambda item: item[1], reverse=True)
            return "; ".join(f"{name}: {_format_number(ms)} ms" for name, ms in readings)

        for key in ("response_time_ms", "avg_response_time_ms"):
            response = parse_response_ms(data.get(key))
            if response is not None:
                return f"Response time: {_format_number(response)} ms"

        # Never fall through to the raw extra_data dump for this check.
        return _not_measured("no instance response time could be read from SMLG")

    # ---------------------------------------------------------------
    # SM66 - Work processes (global)
    #
    # There was no SM66 branch here at all, so the cell carried the whole
    # extra_data dict. What a BASIS reader wants off this screen is narrow:
    # how many work processes are actually running, and whether any are in
    # PRIV mode or have been running too long.
    # ---------------------------------------------------------------
    if tcode == "SM66":
        running = _number(data.get("running_processes"))
        on_hold = _number(data.get("on_hold_processes"))
        waiting = _number(data.get("waiting_processes"))
        priv = _number(data.get("priv_mode_processes"))
        active = _number(data.get("active_processes"))
        if active is None and running is not None and on_hold is not None:
            active = running + on_hold

        def figure(value):
            return _format_number(value) if value is not None else "not read"

        if active is not None or running is not None:
            head = (f"Active: {figure(active)} (Running {figure(running)}, "
                    f"On Hold {figure(on_hold)})" if active is not None
                    else f"Running: {figure(running)}")
            return f"{head}; Waiting: {figure(waiting)}; PRIV mode: {figure(priv)}"

    # ---------------------------------------------------------------
    # SMQ1 / SMQ2
    # ---------------------------------------------------------------
    if tcode in ("SMQ1", "SMQ2"):
        entries = data.get("entries_displayed")
        queues = data.get("queues_displayed")

        if entries is not None or queues is not None:
            parts = []

            if entries is not None:
                parts.append(
                    f"Number of Entries Displayed: {_format_number(entries)}"
                )

            if queues is not None:
                parts.append(
                    f"Number of Queues Displayed: {_format_number(queues)}"
                )

            return "; ".join(parts)

    # ---------------------------------------------------------------
    # SOST
    # ---------------------------------------------------------------
    if tcode == "SOST":
        send_requests = data.get("send_requests")
        waiting = data.get("waiting")
        sent = data.get("sent")
        errors = data.get("errors")

        values = []

        if send_requests is not None:
            values.append(
                f"Send Requests: {_format_number(send_requests)}"
            )

        if waiting is not None:
            values.append(
                f"Waiting: {_format_number(waiting)}"
            )

        if sent is not None:
            values.append(
                f"Sent: {_format_number(sent)}"
            )

        if errors is not None:
            values.append(
                f"Errors: {_format_number(errors)}"
            )

        if values:
            return "; ".join(values)

    # ---------------------------------------------------------------
    # SP01 - Spool Requests
    # ---------------------------------------------------------------
    if tcode == "SP01":
        requests = _number(data.get("spool_requests"))
        errors = _number(data.get("spool_errors"))
        without_output = _number(data.get("spool_without_output_request"))

        if requests is None:
            at_least = _number(data.get("spool_requests_at_least"))
            if at_least:
                return _not_measured(f"list only partly read (at least {_format_number(at_least)} requests)")
            return _not_measured("the spool list could not be read")

        text = f"{_format_number(requests)} spool requests"
        selection = data.get("selection") or {}
        if selection.get("created_by") == "*":
            text += " (created today, all users)"
        if without_output is not None:
            text += f"; {_format_number(without_output)} without output request"
        if errors is not None:
            text += f"; {_format_number(errors)} in error"
        return text

    # ---------------------------------------------------------------
    # ST22
    # ---------------------------------------------------------------
    if tcode == "ST22":
        count = data.get("dump_count")

        if count is not None:
            return (
                f"{_format_number(count)} ABAP dumps"
            )

    # ---------------------------------------------------------------
    # ST06
    # ---------------------------------------------------------------
    if tcode == "ST06":
        summary = data.get("host_summary")

        if summary:
            return _clean(summary)

        # Common structured host values.
        parts = []

        for key, label in (
            ("cpu_utilization", "CPU"),
            ("memory_utilization", "Memory"),
            ("disk_utilization", "Disk"),
            ("host_status", "Host Status"),
        ):
            value = data.get(key)

            if value not in (None, ""):
                parts.append(
                    f"{label}: {_clean(value)}"
                )

        if parts:
            return "; ".join(parts)

    # ---------------------------------------------------------------
    # Generic known fields
    # ---------------------------------------------------------------
    preferred_keys = [
        "summary",
        "status",
        "message",
        "description",
        "result",
        "observation",
        "backup_status",
        "database_status",
        "space_status",
        "smtp_status",
        "system_log_summary",
        "spool_summary",
        "host_summary",
    ]

    for key in preferred_keys:
        value = data.get(key)

        if value not in (None, ""):
            return _clean(value)

    # ---------------------------------------------------------------
    # Last-resort structured formatting
    # ---------------------------------------------------------------
    ignored_keys = {
        "evidence_id",
        "screenshot",
        "screenshot_path",
        "screenshot_captured",
        "ocr_text",
        "ocr_excerpt",
        "raw_ocr",
        "gui_text",
    }

    parts = []

    for key, value in data.items():

        if key in ignored_keys:
            continue

        if value in (None, ""):
            continue

        if isinstance(value, (dict, list)):
            continue

        label = key.replace("_", " ").title()

        parts.append(
            f"{label}: {_clean(value)}"
        )

    return "; ".join(parts) if parts else NO_DATA_TEXT

# ---------------------------------------------------------------------------
# Observation
#
# The Observation column says what a reading MEANS -- impact or next step --
# rather than repeating the Actual Result beside it. The PDF prints both
# columns side by side, and a row reading "SMTP Mail Port: 25 | SMTP Mail
# Port: 25" is what made the report look unfinished.
# ---------------------------------------------------------------------------

def _observation_override(tcode: str, data: dict, status: str):
    if data.get("extraction_failed") or status == "UNKNOWN":
        return ("Compare with the screenshot. If it shows data, re-run the sweep; "
                "if this repeats, the screen reader needs updating for this system.")

    if tcode == "AL08":
        return "System-wide logon snapshot; no user-load threshold is configured."

    if tcode == "DB02":
        worst = None
        for label, group, key in _DB02_ITEMS:
            pct = _usage((data.get(group) or {}).get(key)).get("usage_percent")
            if pct is not None and (worst is None or pct > worst[1]):
                worst = (label, float(pct))
        state = _clean(data.get("operational_state"))
        alerts = [a for a in (data.get("alerts") or []) if "no alert" not in str(a).lower()]
        parts = []
        if state:
            parts.append(f"{state}.")
        if worst:
            parts.append(f"Highest utilisation: {worst[0]} at {worst[1]:.1f}%.")
        parts.append(f"{len(alerts)} database alert(s) open." if alerts else "No database alerts.")
        return " ".join(parts)

    if tcode == "DB12":
        hours = _backup_age_hours(data)
        running = _clean((data.get("running_backup") or {}).get("start_time"))
        if hours is not None and hours >= 0:
            age = f"{hours * 60:.0f} min" if hours < 2 else f"{hours:.0f} h"
            text = (f"Last successful backup is {age} old"
                    + (f", over the {DB12_MAX_BACKUP_AGE_HOURS} h limit." if hours > DB12_MAX_BACKUP_AGE_HOURS
                       else "; within the daily cycle."))
            if running:
                text += f" Today's backup is in progress (started {running[-8:]})."
            return text

    if tcode == "SCOT":
        if status == "OK":
            return "SMTP node is configured, so outbound mail has a route out of the system."
        return ("Outbound mail cannot leave the system while no SMTP node is configured; "
                "SOST send requests stay queued. Create and activate a node in SCOT.")

    if tcode == "SM12":
        count = _number(data.get("lock_count"))
        limits = _threshold_limits("SM12")
        if count is not None and limits and None not in limits:
            warning, critical = limits
            if count >= critical:
                return (f"Above the {_format_number(critical)}-entry critical level; look for "
                        "long-held locks and terminated sessions still holding entries.")
            if count >= warning:
                return f"Above the {_format_number(warning)}-entry warning level."
            return f"Below the {_format_number(warning)}-entry warning level."

    if tcode == "SM21":
        logs = _number(data.get("log_count"))
        errors = _number(data.get("error_count")) or 0
        warnings = _number(data.get("warning_count")) or 0
        if logs is not None:
            if errors:
                text = f"{_format_number(errors)} error-level entr{'y' if errors == 1 else 'ies'}"
                first = next((e for e in (data.get("error_entries") or []) if isinstance(e, dict)), None)
                if first:
                    when = " ".join(x for x in (_clean(first.get("id")),
                                                f"at {_clean(first.get('time'))}" if first.get("time") else "",
                                                f"user {_clean(first.get('user'))}" if first.get("user") else "") if x)
                    text += f": {when} — {_clean(first.get('text'))}" if when else f": {_clean(first.get('text'))}"
                    if errors > 1:
                        text += f" (+{_format_number(errors - 1)} more)"
                return text + ". Review in SM21 and correlate with ST22 at the same times."
            if warnings:
                return (f"{_format_number(warnings)} of {_format_number(logs)} entries are "
                        "warning-level; no error-level entries.")
            routine = _number(data.get("routine_count")) or 0
            echoes = _number(data.get("dump_echo_count")) or 0
            if routine or echoes:
                return ("No error- or warning-level entries. Routine entries (client "
                        "disconnects, soft-cancels, work-process restarts) and dump "
                        "messages (graded in ST22) are counted but do not alert.")
            return "No error- or warning-level entries."

    if tcode == "SM13" and _number(data.get("failed_update_count")) is not None:
        errors = _number(data.get("error_update_count")) or 0
        stale = _number(data.get("stale_initial_count")) or 0
        pending = _number(data.get("pending_update_count")) or 0
        if errors:
            return (f"{_format_number(errors)} update(s) in error: display the error in SM13, "
                    "fix the cause, then repeat or delete the update.")
        if stale:
            return ("Update(s) waiting in Initial for more than 10 minutes: check the "
                    "update work processes (SM50, type UPD) and SM13 > Administration.")
        if pending:
            return "Updates in progress; normal unless they are still Initial on the next run."
        return "No pending or failed update requests."

    if tcode == "SM37":
        if _number(data.get("active_job_count")) is not None or _number(data.get("job_count")) is not None:
            return "Active jobs are expected; this check lists them for context and does not alert."

    if tcode == "SM37_CANCELLED":
        count = _number(data.get("cancelled_job_count"))
        if count == 0:
            return "No background job was cancelled in the selection window."
        if count:
            return "Open each job log (SM37 > Job log) and check ST22 for a dump at the cancel time."

    if tcode == "SMLG":
        from sap_gui.smlg_analyzer import parse_response_ms

        values = [parse_response_ms(i.get("response_time_ms")) for i in (data.get("instances") or [])]
        values = [v for v in values if v is not None]
        if values:
            worst = max(values)
            if worst > 2500:
                return f"Worst instance at {_format_number(worst)} ms is above the 2,500 ms emergency band; investigate now."
            if worst > 2000:
                return f"Worst instance at {_format_number(worst)} ms is above the 2,000 ms critical band."
            if worst > 1500:
                return (f"Worst instance at {_format_number(worst)} ms is above the 1,500 ms warning band; "
                        "check work-process load (SM50/SM66) and ST03N.")
            return "All instances within the 1,500 ms response-time target."

    if tcode == "SOST":
        send = _number(data.get("send_requests"))
        sent = _number(data.get("sent"))
        errors = _number(data.get("errors")) or 0
        if send == 0:
            return "No outbound send requests in the period."
        if send and sent == 0:
            return ("Requests are queued but none were sent; check the SMTP node in SCOT "
                    "and the SAPconnect send job (RSCONN01).")
        if errors:
            return f"{_format_number(errors)} send request(s) in error; review them in SOST."

    if tcode == "SP01":
        errors = _number(data.get("spool_errors")) or 0
        if _number(data.get("spool_requests")) is not None:
            if errors:
                return f"{_format_number(errors)} spool request(s) in error; check the output devices (SPAD)."
            return "No spool requests in error."

    if tcode == "ST22":
        dumps = [d for d in (data.get("dumps") or []) if isinstance(d, dict)]
        if dumps:
            groups = {}
            for d in dumps:
                g = groups.setdefault(_clean(d.get("runtime_error")) or "Runtime error",
                                      {"n": 0, "programs": set(), "users": set()})
                g["n"] += 1
                if d.get("program"):
                    g["programs"].add(_short_program(d.get("program")))
                if d.get("user"):
                    g["users"].add(_clean(d.get("user")))
            parts = []
            for name, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:4]:
                detail = ", ".join(sorted(g["programs"])[:2])
                if g["users"]:
                    detail += (", " if detail else "") + "user " + ", ".join(sorted(g["users"])[:2])
                parts.append(f"{name} ×{g['n']}" + (f" ({detail})" if detail else ""))
            return "; ".join(parts) + "."

    if tcode == "SM58":
        texts = [x for x in (data.get("status_texts") or []) if isinstance(x, dict)]
        failed = _number((data.get("information") or {}).get("failed_entries", data.get("failed_entries")))
        if texts:
            modules = sorted({_clean(r.get("function_module")) for r in (data.get("trfc_rows") or [])
                              if isinstance(r, dict) and r.get("function_module")})
            text = "; ".join(f"{x['count']}× {x['text']}" for x in texts[:2])
            if modules:
                text += f" ({', '.join(modules[:2])})"
            if failed:
                text += ". Fix the cause, then re-process the entries in SM58."
            return text

    if tcode == "SM66":
        priv = _number(data.get("priv_mode_processes"))
        waiting = _number(data.get("waiting_processes"))
        if priv:
            return (f"{_format_number(priv)} work process(es) in PRIV mode: a user context has left "
                    "shared memory; check the program and user in SM66 and memory in ST02.")
        if priv is not None:
            text = "No work processes in PRIV mode."
            if waiting == 0:
                text += " No Waiting (free) work processes at capture time."
            return text

    return None


def _build_observation(
    metric: MetricResult,
    status: str,
) -> str:
    """
    Build a concise deterministic observation for Excel.
    """

    tcode = _clean(
        getattr(metric, "tcode", "")
    ).upper()

    data = _get_data(metric)

    override = _observation_override(tcode, data, status)
    if override:
        return override

    # ---------------------------------------------------------------
    # DB01
    # ---------------------------------------------------------------
    if tcode == "DB01":
        locks = _number(data.get("lock_count"))
        deadlocks = _number(data.get("deadlock_count"))

        if deadlocks is not None and deadlocks > 0:
            return (
                f"{_format_number(deadlocks)} database deadlock(s) "
                "detected; immediate investigation is required."
            )

        if locks is not None and locks > 0:
            return (
                f"{_format_number(locks)} database lock(s) detected; "
                "review lock ownership and contention."
            )

        if locks == 0 and deadlocks == 0:
            return "No database locks or deadlocks detected."

    # ---------------------------------------------------------------
    # DB02
    # ---------------------------------------------------------------
    if tcode == "DB02":
        actual = _build_actual_result(metric)
        if actual != NO_DATA_TEXT:
            return f"Database memory and storage captured: {actual}."


    # ---------------------------------------------------------------
    # DB12
    # ---------------------------------------------------------------
    if tcode == "DB12":
        latest = data.get("latest_backup") or {}
        end_time = _clean(latest.get("end_time"))
        if end_time:
            return f"Last backup date/time: {end_time}."


    # ---------------------------------------------------------------
    # SCOT
    # ---------------------------------------------------------------
    if tcode == "SCOT":
        smtp_status = _clean(
            data.get("smtp_status")
        )

        active = _number(
            data.get("active_nodes")
        )

        nodes = _number(
            data.get("node_count")
        )

        if smtp_status:
            if status == "CRITICAL":
                return (
                    f"SMTP/SAPconnect status is {smtp_status}; "
                    "mail configuration requires immediate attention."
                )

            if status == "WARNING":
                return (
                    f"SMTP/SAPconnect status is {smtp_status}; "
                    "review configured mail nodes."
                )

            if active is not None:
                return (
                    f"SMTP/SAPconnect status: {smtp_status}; "
                    f"{_format_number(active)} active node(s)."
                )

            return (
                f"SMTP/SAPconnect status: {smtp_status}."
            )

        if nodes is not None:
            return (
                f"{_format_number(nodes)} SAPconnect node(s) detected; "
                f"{_format_number(active or 0)} active."
            )

    # ---------------------------------------------------------------
    # SM21
    # ---------------------------------------------------------------
    if tcode == "SM21":
        logs = _number(data.get("log_count"))
        errors = _number(data.get("error_count"))
        warnings = _number(data.get("warning_count"))

        if errors is not None and errors > 0:
            return (
                f"{_format_number(errors)} system-log error(s) detected; "
                "review SM21 details."
            )

        if warnings is not None and warnings > 0:
            return (
                f"{_format_number(warnings)} system-log warning(s) "
                "detected; review SM21 details."
            )

        if logs == 0:
            return "No system-log entries detected."

        if logs is not None:
            return (
                f"{_format_number(logs)} system-log entr"
                f"{'y' if logs == 1 else 'ies'} reviewed; "
                "no error or warning condition detected."
            )

    # ---------------------------------------------------------------
    # SMLG
    # ---------------------------------------------------------------
    if tcode == "SMLG":
        instances = data.get("instances") or []

        if not instances:
            return (
                "No SMLG application-server response-time "
                "data was captured."
            )

        valid = [
            x
            for x in instances
            if _number(x.get("response_time_ms")) is not None
        ]

        if not valid:
            return (
                "SMLG instances were detected, but response time "
                "was unavailable."
            )

        worst = max(
            valid,
            key=lambda x: float(
                _number(x.get("response_time_ms"))
            ),
        )

        response = float(
            _number(
                worst.get("response_time_ms")
            )
        )

        instance = _clean(
            worst.get("instance")
        )

        if status == "CRITICAL":
            condition = "critical"

        elif status == "WARNING":
            condition = "above the warning threshold"

        else:
            condition = "within the normal range"

        return (
            f"{len(valid)} application server instance(s) monitored. "
            f"Worst response time is "
            f"{_format_number(response)} ms on {instance}; "
            f"performance is {condition}."
        )

    # ---------------------------------------------------------------
    # SM58
    # ---------------------------------------------------------------
    if tcode == "SM58":
        information = data.get("information") or {}
        count = _number(information.get("entries_displayed", data.get("trfc_count")))
        failed = _number(information.get("failed_entries", data.get("failed_entries")))
        executing = _number(information.get("entries_in_execution", data.get("entries_in_execution")))

        if failed is not None and failed > 0:
            return (
                f"{_format_number(failed)} failed tRFC entries detected; "
                "investigation is required."
            )

        if count is not None:
            if count == 0:
                return "No tRFC entries are currently displayed."

            execution_text = (
                f" {_format_number(executing)} in execution."
                if executing is not None else ""
            )
            return (
                f"{_format_number(count)} tRFC entries displayed; "
                f"no failed entries detected.{execution_text}"
            )

    # ---------------------------------------------------------------
    # SM12
    # ---------------------------------------------------------------
    if tcode == "SM12":
        count = _number(
            data.get("lock_count")
        )

        if count is not None:
            if count == 0:
                return "No lock entries detected."

            return (
                f"{_format_number(count)} lock entries detected."
            )

    # ---------------------------------------------------------------
    # SM13
    # ---------------------------------------------------------------
    if tcode == "SM13":
        count = _number(
            data.get("update_count")
        )

        if count is not None:
            if count == 0:
                return "No update errors detected."

            return (
                f"{_format_number(count)} update records "
                "require attention."
            )

        summary = data.get("update_summary")

        if summary:
            return _clean(summary)

    # ---------------------------------------------------------------
    # SM37
    # ---------------------------------------------------------------
    if tcode == "SM37":
        count = _number(data.get("active_job_count"))
        if count is None:
            count = _number(data.get("active_jobs"))

        if count is not None:
            return (
                f"{_format_number(count)} active "
                "background job(s) detected."
            )

    # ---------------------------------------------------------------
    # Cancelled jobs
    # ---------------------------------------------------------------
    if tcode == "SM37_CANCELLED":
        count = _number(data.get("cancelled_job_count"))
        if count is None:
            count = _number(data.get("cancelled_jobs"))

        if count is not None:
            if count == 0:
                return (
                    "No cancelled background jobs detected."
                )

            return (
                f"{_format_number(count)} cancelled "
                "background job(s) detected."
            )

    # ---------------------------------------------------------------
    # SM66
    # ---------------------------------------------------------------
    if tcode == "SM66":
        total = data.get("total_processes")

        if total is None:
            total = data.get("visible_process_rows")

        running = data.get("running_processes")
        held = data.get("held_processes")
        failed = data.get("failed_processes")

        if total is not None:
            parts = [
                f"{_format_number(total)} processes displayed"
            ]

            if running is not None:
                parts.append(
                    f"{_format_number(running)} Running"
                )

            if held is not None and _number(held) > 0:
                parts.append(
                    f"{_format_number(held)} On Hold"
                )

            if failed is not None and _number(failed) > 0:
                parts.append(
                    f"{_format_number(failed)} Failed"
                )

            return "; ".join(parts) + "."

    # ---------------------------------------------------------------
    # SM51
    # ---------------------------------------------------------------
    if tcode == "SM51":
        count = data.get("instances_started")

        if count is None:
            count = data.get("instance_count")

        if count is not None:
            return (
                f"{_format_number(count)} active "
                "application server"
                f"{'s' if _number(count) != 1 else ''}."
            )

    # ---------------------------------------------------------------
    # SMQ1 / SMQ2
    # ---------------------------------------------------------------
    if tcode in ("SMQ1", "SMQ2"):
        entries = _number(
            data.get("entries_displayed")
        )

        queues = _number(
            data.get("queues_displayed")
        )

        if entries is not None:
            if entries == 0:
                if queues is not None:
                    return (
                        f"No queue entries displayed; "
                        f"{_format_number(queues)} queue(s) displayed."
                    )

                return "No queue entries displayed."

            return (
                f"SMQ information: Number of Entries Displayed = "
                f"{_format_number(entries)}; "
                + (
                    f"Number of Queues Displayed = {_format_number(queues)}."
                    if queues is not None
                    else "Number of Queues Displayed is unavailable."
                )
            )

    # ---------------------------------------------------------------
    # SOST
    # ---------------------------------------------------------------
    if tcode == "SOST":
        send_requests = _number(data.get("send_requests"))
        waiting = _number(data.get("waiting"))
        sent = _number(data.get("sent"))
        errors = _number(data.get("errors"))

        parts = []
        if send_requests is not None:
            parts.append(f"Send Requests = {_format_number(send_requests)}")
        if waiting is not None:
            parts.append(f"Waiting = {_format_number(waiting)}")
        if sent is not None:
            parts.append(f"Sent = {_format_number(sent)}")
        if errors is not None:
            parts.append(f"Errors = {_format_number(errors)}")

        if parts:
            return "SOST information: " + "; ".join(parts) + "."

    # ---------------------------------------------------------------
    # ST22
    # ---------------------------------------------------------------
    if tcode == "ST22":
        count = _number(
            data.get("dump_count")
        )

        if count is not None:
            if count == 0:
                return "No ABAP dumps detected."

            return (
                f"{_format_number(count)} ABAP dumps detected."
            )

    # ---------------------------------------------------------------
    # ST06
    # ---------------------------------------------------------------
    if tcode == "ST06":
        summary = data.get("host_summary")

        if summary:
            return _clean(summary)

        parts = []

        for key, label in (
            ("cpu_utilization", "CPU"),
            ("memory_utilization", "Memory"),
            ("disk_utilization", "Disk"),
            ("host_status", "Host Status"),
        ):
            value = data.get(key)

            if value not in (None, ""):
                parts.append(
                    f"{label}: {_clean(value)}"
                )

        if parts:
            return "; ".join(parts) + "."

    # ---------------------------------------------------------------
    # Generic
    # ---------------------------------------------------------------
    if status == "UNKNOWN":
        return (
            "Structured monitoring value was not available; "
            "refer to the attached screenshot."
        )

    return (
        f"{tcode} monitoring completed with status {status}."
    )


# ---------------------------------------------------------------------------
# Check name
# ---------------------------------------------------------------------------

def _check_name(tcode: str) -> str:

    names = {
        "AL08": "Logged-On Users",
        "DB01": "Database Locks / Deadlocks",
        "DB02": "Database Space / Storage",
        "DB12": "Database Backup Status",
        "SCOT": "SAPconnect / SMTP Status",
        "SM12": "Lock Entries",
        "SM13": "Update Requests",
        "SM21": "System Log",
        "SM37": "Background Jobs",
        "SM37_CANCELLED": "Cancelled Jobs",
        "SM51": "Instance / Work Process Health",
        "SM58": "tRFC Backlog / Errors",
        "SM66": "Active / Long Running Work Processes",
        "SMLG": "Logon Groups / Response Time",
        "SMQ1": "Outbound qRFC Queue",
        "SMQ2": "Inbound qRFC Queue",
        "SOST": "SAPconnect Send Requests",
        "SP01": "Spool Requests",
        "ST22": "ABAP Dumps",
        "ST06": "OS / Host Health",
    }

    return names.get(
        tcode,
        tcode,
    )


# ---------------------------------------------------------------------------
# Narrative object
# ---------------------------------------------------------------------------

class Narrative:
    def __init__(
        self,
        tcode: str,
        actual_result: str,
        status: str,
        observation: str,
    ):
        self.tcode = tcode
        self.actual_result = actual_result
        self.status = status
        self.observation = observation


def _narrate_metric(metric: MetricResult) -> Narrative:

    tcode = _clean(
        getattr(metric, "tcode", "")
    ).upper()

    actual = _build_actual_result(metric)

    status = _status_for_metric(metric)

    observation = _build_observation(
        metric,
        status,
    )

    return Narrative(
        tcode=tcode,
        actual_result=actual,
        status=status,
        observation=observation,
    )


def narrate_all(
    gui_results: list[MetricResult],
) -> list[Narrative]:

    narratives = []

    for metric in gui_results:

        try:
            narratives.append(
                _narrate_metric(metric)
            )

        except Exception:
            tcode = _clean(
                getattr(metric, "tcode", "")
            ).upper()

            narratives.append(
                Narrative(
                    tcode=tcode,
                    actual_result=NO_DATA_TEXT,
                    status="UNKNOWN",
                    observation=(
                        "Unable to build structured "
                        "monitoring result."
                    ),
                )
            )

    return narratives


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summary_counts(
    narratives: list[Narrative],
) -> dict:

    total = len(narratives)

    captured = sum(
        1
        for n in narratives
        if n.actual_result != NO_DATA_TEXT
    )

    attention = sum(
        1
        for n in narratives
        if n.status in ("WARNING", "CRITICAL")
    )

    failed = sum(
        1
        for n in narratives
        if n.status == "CRITICAL"
    )

    return {
        "total": total,
        "captured": captured,
        "attention": attention,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Deterministic analysis
# ---------------------------------------------------------------------------

def deterministic_analysis(
    metric: MetricResult,
) -> dict:

    status = _status_for_metric(metric)

    return {
        "status": status,
        "actual_result": _build_actual_result(metric),
        "observation": _build_observation(
            metric,
            status,
        ),
    }


# ---------------------------------------------------------------------------
# Main Excel writer
# ---------------------------------------------------------------------------

def fill_metrobrands_template(
    template_path: str,
    output_path: str,
    gui_results: list[MetricResult],
    report_date: str = None,
    result=None,
) -> str:
    """
    Populate the InfraBeatOps monitoring template.

    Despite the historical function name, this writes the current
    InfraBeatOps template.
    """

    if not os.path.exists(template_path):
        raise FileNotFoundError(
            f"Excel template not found: {template_path}"
        )

    output_dir = os.path.dirname(
        os.path.abspath(output_path)
    )

    os.makedirs(
        output_dir,
        exist_ok=True,
    )

    # ---------------------------------------------------------------
    # Copy template
    # ---------------------------------------------------------------

    shutil.copy(
        template_path,
        output_path,
    )

    wb = openpyxl.load_workbook(
        output_path
    )

    # ---------------------------------------------------------------
    # Find actual monitoring sheet
    # ---------------------------------------------------------------

    if "Monitoring Sheet" in wb.sheetnames:
        ws = wb["Monitoring Sheet"]

    elif "System Monitoring" in wb.sheetnames:
        ws = wb["System Monitoring"]

    else:
        ws = wb[wb.sheetnames[0]]

    # ---------------------------------------------------------------
    # Date
    # ---------------------------------------------------------------

    if report_date is None:
        report_date = datetime.now().strftime(
            "%d.%m.%Y"
        )

    # ---------------------------------------------------------------
    # Map GUI results by T-code
    # ---------------------------------------------------------------

    by_tcode = {}

    for metric in gui_results:

        tcode = _clean(
            getattr(metric, "tcode", "")
        ).upper()

        if tcode:
            by_tcode[tcode] = metric

    # ---------------------------------------------------------------
    # System information
    # ---------------------------------------------------------------

    system_name = ""

    client = ""

    sid = ""

    environment = ""

    for metric in gui_results:

        data = _get_data(metric)

        if not system_name:
            system_name = _clean(
                data.get("system")
            )

        if not client:
            client = _clean(
                data.get("client")
            )

        if not sid:
            sid = _clean(
                data.get("sid")
            )

        if not environment:
            environment = _clean(
                data.get("environment")
            )

    if result is not None:

        system_name = (
            _clean(getattr(result, "system", None))
            or system_name
        )

        client = (
            _clean(getattr(result, "client", None))
            or client
        )

        sid = (
            _clean(getattr(result, "sid", None))
            or sid
        )

        environment = (
            _clean(getattr(result, "environment", None))
            or environment
        )

    # The workbook title is dynamic. Never keep the template's company-name
    # placeholder here; use the actual SAP system name and SID from the run.
    if not sid:
        for metric in gui_results:
            data = _get_data(metric)
            sid = _clean(data.get("sid"))
            if sid:
                break

    config = _system_config(system_name)
    sid = sid or _clean(config.get("sap_system_id"))
    environment = environment or _clean(config.get("environment"))

    title = system_name if not sid or sid == system_name else f"{system_name} | {sid}"
    ws["A1"] = f"{title} — SAP BASIS Monitoring"
    ws["A2"] = f"Monitoring checklist — {len(TCODE_ROW_MAP)} SAP T-codes"

    # ---------------------------------------------------------------
    # Write rows
    # ---------------------------------------------------------------

    for row, tcode in TCODE_ROW_MAP.items():

        metric = by_tcode.get(tcode)

        # Date and time of THIS check's capture. The sheet used to stamp
        # every row with the moment the workbook was written, several
        # minutes after the screens were read.
        stamp = _capture_time(metric) if metric is not None else None
        row_date, row_time = stamp or (report_date, datetime.now().strftime("%H:%M:%S"))

        ws.cell(
            row=row,
            column=1,
            value=row_date,
        )

        ws.cell(
            row=row,
            column=2,
            value=row_time,
        )

        ws.cell(
            row=row,
            column=3,
            value=system_name,
        )

        ws.cell(
            row=row,
            column=4,
            value=sid,
        )

        ws.cell(
            row=row,
            column=5,
            value=client,
        )

        ws.cell(
            row=row,
            column=6,
            value=environment or "—",
        )

        ws.cell(
            row=row,
            column=7,
            value=tcode,
        )

        ws.cell(
            row=row,
            column=8,
            value=_check_name(tcode),
        )

        # -----------------------------------------------------------
        # No metric
        # -----------------------------------------------------------

        if metric is None:

            ws.cell(
                row=row,
                column=9,
                value=NO_DATA_TEXT,
            )

            ws.cell(
                row=row,
                column=10,
                value="UNKNOWN",
            )

            ws.cell(
                row=row,
                column=11,
                value=(
                    "No structured result was returned "
                    "for this check."
                ),
            )

            continue

        # -----------------------------------------------------------
        # Actual structured result
        # -----------------------------------------------------------

        actual_result = _build_actual_result(
            metric
        )

        status = _status_for_metric(
            metric
        )

        observation = _build_observation(
            metric,
            status,
        )

        ws.cell(
            row=row,
            column=9,
            value=actual_result,
        )

        ws.cell(
            row=row,
            column=10,
            value=status,
        )

        ws.cell(
            row=row,
            column=11,
            value=observation,
        )

    # ----------------------------------------------------------------
    # Formatting
    # ----------------------------------------------------------------

    header_row = 3

    for column in range(1, 12):

        cell = ws.cell(
            row=header_row,
            column=column,
        )

        cell.font = Font(
            bold=True
        )

        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    # Widths for the actual 11-column template
    widths = {
        1: 14,  # Date
        2: 12,  # Time
        3: 18,  # System
        4: 10,  # SID
        5: 10,  # Client
        6: 14,  # Environment
        7: 20,  # T-Code
        8: 38,  # Check
        9: 55,  # Actual Result
        10: 14, # Status
        11: 65, # Observation
    }

    for column, width in widths.items():

        ws.column_dimensions[
            get_column_letter(column)
        ].width = width

    # ---------------------------------------------------------------
    # Data formatting
    # ---------------------------------------------------------------

    status_fills = {
        "OK": PatternFill(
            fill_type="solid",
            fgColor="C6EFCE",
        ),
        "WARNING": PatternFill(
            fill_type="solid",
            fgColor="FFEB9C",
        ),
        "CRITICAL": PatternFill(
            fill_type="solid",
            fgColor="FFC7CE",
        ),
        "UNKNOWN": PatternFill(
            fill_type="solid",
            fgColor="D9E1F2",
        ),
    }

    for row in range(
        4,
        max(TCODE_ROW_MAP.keys()) + 1,
    ):

        for column in range(1, 12):

            ws.cell(
                row=row,
                column=column,
            ).alignment = Alignment(
                vertical="top",
                wrap_text=True,
            )

        status_cell = ws.cell(
            row=row,
            column=10,
        )

        status = _clean(
            status_cell.value
        ).upper()

        if status in status_fills:
            status_cell.fill = (
                status_fills[status]
            )

        status_cell.font = Font(
            bold=True
        )

    # ---------------------------------------------------------------
    # Freeze header
    # ---------------------------------------------------------------

    ws.freeze_panes = "A4"

    # The guide must use the sheet's own vocabulary: rows say OK, the guide
    # said HEALTHY.
    if "Status Guide" in wb.sheetnames:
        guide = wb["Status Guide"]
        for guide_row in guide.iter_rows(min_row=2):
            cell = guide_row[0]
            if _clean(cell.value).upper() == "HEALTHY":
                cell.value = "OK"
            fill = status_fills.get(_clean(cell.value).upper())
            if fill is not None:
                cell.fill = fill

    # ---------------------------------------------------------------
    # Auto-filter
    # ---------------------------------------------------------------

    ws.auto_filter.ref = (
        f"A3:K{max(TCODE_ROW_MAP.keys())}"
    )

    # ---------------------------------------------------------------
    # Save
    # ---------------------------------------------------------------

    wb.save(output_path)

    return output_path
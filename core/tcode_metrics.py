"""
Normalize SAP GUI T-code extraction into first-class monitoring metrics.

The GUI collector remains responsible for navigation and evidence capture.
This module converts GUI extra_data into stable MetricResult objects so
thresholds, history, correlation, AI analysis, and the dashboard can use
consistent metric names.
"""

from __future__ import annotations

from typing import Any

from core.models import MetricResult, Status


# ---------------------------------------------------------------------------
# SAP GUI metric definitions
#
# Format:
#   extra_data key,
#   normalized metric name,
#   display label,
#   unit,
#   category
# ---------------------------------------------------------------------------

_METRIC_DEFINITIONS: dict[str, list[tuple[str, str, str, str, str]]] = {

    "AL08": [
        (
            "back_end_sessions",
            "sap.al08.backend_sessions",
            "Back-end sessions",
            "count",
            "sessions",
        ),
        (
            "user_logons",
            "sap.al08.user_logons",
            "User logons",
            "count",
            "sessions",
        ),
    ],

    "SM12": [
        (
            "lock_count",
            "sap.sm12.lock_count",
            "SAP locks",
            "count",
            "locks",
        ),
    ],

    "SM13": [
        (
            "update_count",
            "sap.sm13.failed_updates",
            "Failed updates",
            "count",
            "updates",
        ),
    ],

    "SM37": [
        (
            "active_jobs",
            "sap.sm37.active_jobs",
            "Active jobs",
            "count",
            "jobs",
        ),
        (
            "cancelled_jobs",
            "sap.sm37.cancelled_jobs",
            "Cancelled jobs",
            "count",
            "jobs",
        ),
    ],

    "SM51": [
        (
            "instances_started",
            "sap.sm51.instances_started",
            "Application servers",
            "count",
            "runtime",
        ),
    ],

    # -----------------------------------------------------------------------
    # SM50
    # Work-process utilization
    # -----------------------------------------------------------------------
    "SM50": [
        (
            "active_utilization_percent",
            "sap.sm50.wp_utilization",
            "Work process utilization",
            "%",
            "work_processes",
        ),
    ],

    "SM66": [
        (
            "visible_process_rows",
            "sap.sm66.process_rows",
            "Visible work processes",
            "count",
            "work_processes",
        ),
        (
            "running_processes",
            "sap.sm66.running_processes",
            "Running work processes",
            "count",
            "work_processes",
        ),
    ],

    "SMQ1": [
        (
            "entries_displayed",
            "sap.smq1.entries",
            "qRFC outbound entries",
            "count",
            "queues",
        ),
        (
            "queues_displayed",
            "sap.smq1.queues",
            "qRFC outbound queues",
            "count",
            "queues",
        ),
    ],

    "SMQ2": [
        (
            "entries_displayed",
            "sap.smq2.entries",
            "qRFC inbound entries",
            "count",
            "queues",
        ),
        (
            "queues_displayed",
            "sap.smq2.queues",
            "qRFC inbound queues",
            "count",
            "queues",
        ),
    ],

    "SOST": [
        (
            "send_requests",
            "sap.sost.send_requests",
            "Send requests",
            "count",
            "email",
        ),
        (
            "waiting",
            "sap.sost.waiting",
            "Waiting email requests",
            "count",
            "email",
        ),
        (
            "sent",
            "sap.sost.sent",
            "Sent email requests",
            "count",
            "email",
        ),
        (
            "errors",
            "sap.sost.errors",
            "Email errors",
            "count",
            "email",
        ),
    ],

    "SP01": [
        (
            "spool_count_visible",
            "sap.sp01.spool_count",
            "Visible spool requests",
            "count",
            "spool",
        ),
    ],

    # -----------------------------------------------------------------------
    # SMLG
    # Instance response time
    #
    # Uses avg_response_time_ms from action_smlg().
    # Individual instance values remain available in GUI evidence.
    # -----------------------------------------------------------------------
    "SMLG": [
        (
            "avg_response_time_ms",
            "sap.smlg.response_time",
            "Instance response time",
            "ms",
            "performance",
        ),
    ],

    # Keep ST03N separately.
    "ST03N": [
        (
            "dialog_avg_response_time_ms",
            "sap.st03n.dialog_response_time",
            "Dialog response time",
            "ms",
            "performance",
        ),
    ],

    "ST22": [
        (
            "dump_count",
            "sap.st22.dump_count",
            "ABAP dumps today",
            "count",
            "dumps",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Number conversion
# ---------------------------------------------------------------------------

def _to_number(value: Any) -> float | None:
    """
    Convert supported SAP GUI values to a numeric value.

    Handles:
      10
      10.5
      "10"
      "10.5"
      "1,234"
    """

    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")

        try:
            return float(cleaned)
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Normalize one T-code result
# ---------------------------------------------------------------------------

def normalize_tcode_result(result: MetricResult) -> list[MetricResult]:
    """
    Convert one GUI evidence result into zero or more normalized metrics.
    """

    tcode = (result.tcode or "").upper()

    definitions = _METRIC_DEFINITIONS.get(tcode, [])

    normalized: list[MetricResult] = []

    extra_data = result.extra_data or {}

    for key, name, label, unit, category in definitions:

        raw_value = extra_data.get(key)

        value = _to_number(raw_value)

        if value is None:
            continue

        if value.is_integer():
            display = f"{int(value)} {unit}"
        else:
            display = f"{value:g} {unit}"

        normalized.append(
            MetricResult(
                name=name,
                value=value,
                display_value=display,
                status=Status.UNKNOWN,
                source="sap_gui_collector",
                tcode=tcode,
                detail=(
                    f"Extracted from {tcode}; "
                    f"evidence metric '{key}'."
                ),
                screenshot_path=result.screenshot_path,
                screenshot_paths=list(result.screenshot_paths),
                extra_data={
                    "raw_key": key,
                    "raw_value": raw_value,
                    "evidence_metric": True,
                },
                timestamp=result.timestamp,
                category=category,
                unit=unit,
                metric_type="gauge",
            )
        )

    return normalized


# ---------------------------------------------------------------------------
# Normalize all GUI results
# ---------------------------------------------------------------------------

def normalize_tcode_results(
    results: list[MetricResult],
) -> list[MetricResult]:
    """
    Normalize all GUI results while preserving original evidence records.
    """

    metrics: list[MetricResult] = []

    for result in results:
        metrics.extend(
            normalize_tcode_result(result)
        )

    return metrics
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
        # Errors + Initial past the stale limit. The raw record count
        # (below) is the fallback for results from older collectors; it
        # counted updates still being processed as failed.
        (
            "failed_update_count",
            "sap.sm13.failed_updates",
            "Failed updates",
            "count",
            "updates",
        ),
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
        # The OCR read of the SMLG instance table writes "response_time_ms",
        # not "avg_response_time_ms" -- so on any system where the structured
        # average is unavailable (this one: all five candidate function
        # modules return FU_NOT_FOUND) normalization matched nothing and
        # sap.smlg.response_time never reached the report at all. The figure
        # was captured, graded and then silently dropped. Both keys are
        # accepted now; the duplicate-name guard below keeps it to one row.
        (
            "response_time_ms",
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

    # A metric name can be reachable through more than one evidence key
    # (SMLG below). The first key that yields a value wins; the rest are
    # skipped so one capture never produces two rows for one signal.
    emitted: set[str] = set()

    for key, name, label, unit, category in definitions:

        if name in emitted:
            continue

        raw_value = extra_data.get(key)

        if name == "sap.smlg.response_time":
            # SMLG renders 1265 ms as "1.265". _to_number reads that as
            # 1.265, so the metric under-reported by a factor of a thousand
            # and sat permanently below every alert band.
            from sap_gui.smlg_analyzer import parse_response_ms
            value = parse_response_ms(raw_value)
        else:
            value = _to_number(raw_value)

        if value is None:
            continue

        if value.is_integer():
            display = f"{int(value)} {unit}"
        else:
            display = f"{value:g} {unit}"

        # SMLG response time is graded here rather than being left UNKNOWN
        # for the generic threshold engine, because that engine understands
        # only warning/critical and SMLG has a third "flashing" band. See
        # sap_gui/smlg_analyzer.py.
        status = Status.UNKNOWN
        warn = crit = None
        smlg_extra: dict[str, Any] = {}

        if name == "sap.smlg.response_time":
            from sap_gui.smlg_analyzer import (
                SMLG_THRESHOLDS, classify_response_time,
            )
            grade = classify_response_time(value)
            status = grade["status"]
            warn = SMLG_THRESHOLDS["warning_ms"]
            crit = SMLG_THRESHOLDS["critical_ms"]
            smlg_extra = {
                "alert_level": grade["alert_level"],
                "threshold_band": grade["threshold"],
                "emergency_ms": SMLG_THRESHOLDS["emergency_ms"],
            }

        emitted.add(name)

        normalized.append(
            MetricResult(
                name=name,
                value=value,
                display_value=display,
                status=status,
                threshold_warning=warn,
                threshold_critical=crit,
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
                    **smlg_extra,
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

def merge_normalized_metrics(
    existing: list[MetricResult],
    normalized: list[MetricResult],
) -> tuple[list[MetricResult], list[str]]:
    """
    Merge GUI-normalized metrics into a metric list, one row per name.

    The GUI collector deliberately reuses the metric names the RFC
    collector emits -- sap.sm12.lock_count, sap.st22.dump_count and so on
    -- so that one signal keeps one history. But the merge at the call
    site was a bare list.extend(), so a system read over BOTH RFC and SAP
    GUI published every shared metric twice. A real client report carried
    two `sap.sm12.lock_count` rows, reading "2 count" and "3 count", one
    above the other, with nothing to say which was true.

    Most of these are point-in-time gauges of volatile quantities -- an
    enqueue lock can live for seconds -- and the two collectors read them
    at different moments of the same cycle, so a disagreement is usually
    two correct readings taken seconds apart rather than a fault. The
    freshest reading therefore wins, being the best description of "now".
    Where the values actually differ, the discarded reading is recorded in
    the survivor's detail, so a divergence stays visible to whoever reads
    the report instead of being silently resolved.

    Returns the merged list, plus one human-readable note per conflict.
    """

    merged: list[MetricResult] = list(existing)

    first_index: dict[str, int] = {}
    for index, metric in enumerate(merged):
        first_index.setdefault(metric.name, index)

    notes: list[str] = []

    for incoming in normalized:

        index = first_index.get(incoming.name)

        if index is None:
            first_index[incoming.name] = len(merged)
            merged.append(incoming)
            continue

        current = merged[index]

        if incoming.timestamp >= current.timestamp:
            newer, older = incoming, current
        else:
            newer, older = current, incoming

        if _to_number(current.value) != _to_number(incoming.value):
            second = (
                f"Second reading: {older.source or 'another collector'} "
                f"read {older.display_value} at "
                f"{older.timestamp:%H:%M:%S}."
            )
            newer.detail = f"{newer.detail} {second}".strip()
            notes.append(
                f"{newer.name}: kept {newer.display_value} from "
                f"{newer.source or 'unknown source'} at "
                f"{newer.timestamp:%H:%M:%S}; {older.source or 'another '
                'collector'} read {older.display_value} at "
                f"{older.timestamp:%H:%M:%S}."
            )

        merged[index] = newer

    return merged, notes


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
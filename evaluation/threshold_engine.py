"""
Evaluates MetricResult objects against configured thresholds.

Does NOT collect data -- only classifies already-collected numeric metrics
into NORMAL / WARNING / CRITICAL.

Threshold semantics:

    value < warning
        NORMAL

    warning <= value <= critical
        WARNING

    value > critical
        CRITICAL

Metrics that already carry a status are left untouched.
"""

from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__, "monitoring")


def _threshold_key_for_metric(metric_name: str) -> str | None:
    """
    Maps a metric name to its threshold config key.

    Disk metrics are named like:
        disk_/
        disk_/usr/sap

    and therefore all map to:
        disk

    Normalized SAP metrics use their complete metric name.
    """

    if metric_name.startswith("disk_"):
        return "disk"

    return metric_name


def evaluate_metric(
    metric: MetricResult,
    thresholds: dict,
) -> MetricResult:
    """
    Classify one numeric metric using configured thresholds.

    Threshold semantics:

        value < warning
            NORMAL

        warning <= value <= critical
            WARNING

        value > critical
            CRITICAL

    Only UNKNOWN metrics are evaluated.
    Already-classified metrics are preserved.
    """

    # Do not override an existing collector classification.
    if metric.status != Status.UNKNOWN:
        return metric

    # Cannot classify a missing value.
    if metric.value is None:
        log.debug(
            "Metric '%s' has no numeric value -- leaving UNKNOWN.",
            metric.name,
        )
        return metric

    key = _threshold_key_for_metric(metric.name)

    limits = thresholds.get(key)

    if not limits:
        log.debug(
            "No threshold configured for metric '%s' "
            "(key='%s') -- leaving UNKNOWN.",
            metric.name,
            key,
        )
        return metric

    warning = limits.get("warning")
    critical = limits.get("critical")

    # Store thresholds on the metric so the dashboard/API can expose
    # the actual limits used for this observation.
    metric.threshold_warning = warning
    metric.threshold_critical = critical

    # ------------------------------------------------------------------
    # CRITICAL
    #
    # IMPORTANT:
    # Critical is strictly greater than the critical threshold.
    #
    # Example:
    #   critical = 2500
    #
    #   2500  -> WARNING
    #   2501  -> CRITICAL
    # ------------------------------------------------------------------

    if critical is not None and metric.value > critical:
        metric.status = Status.CRITICAL

    # ------------------------------------------------------------------
    # WARNING
    #
    # Warning starts at the warning threshold and continues through
    # the critical threshold.
    #
    # Example:
    #   warning = 1000
    #   critical = 2500
    #
    #   999   -> NORMAL
    #   1000  -> WARNING
    #   2500  -> WARNING
    # ------------------------------------------------------------------

    elif warning is not None and metric.value >= warning:
        metric.status = Status.WARNING

    # ------------------------------------------------------------------
    # NORMAL
    # ------------------------------------------------------------------

    else:
        metric.status = Status.NORMAL

    return metric


def evaluate_all(
    metrics: list[MetricResult],
    thresholds: dict,
) -> list[MetricResult]:
    """
    Evaluate all metrics against configured thresholds.
    """

    return [
        evaluate_metric(metric, thresholds)
        for metric in metrics
    ]
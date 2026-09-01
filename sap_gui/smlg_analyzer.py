"""
SMLG response-time analyzer.

Severity thresholds:

    < 1000 ms       HEALTHY
    1000-2500 ms   WARNING
    > 2500 ms       CRITICAL
"""

from typing import Any


SMLG_THRESHOLDS = {
    "healthy_max_ms": 1000.0,
    "warning_max_ms": 2500.0,
}


SMLG_COLORS = {
    "HEALTHY": "#34d399",
    "WARNING": "#fbbf24",
    "CRITICAL": "#f87171",
}


def classify_response_time(response_time_ms: float) -> dict[str, Any]:
    """
    Classify one SMLG instance response time.
    """

    value = float(response_time_ms)

    if value < SMLG_THRESHOLDS["healthy_max_ms"]:
        severity = "HEALTHY"
        description = "Normal instance response time."
        threshold = "< 1000 ms"

    elif value <= SMLG_THRESHOLDS["warning_max_ms"]:
        severity = "WARNING"
        description = (
            "Elevated instance response time; investigate "
            "database, enqueue, CPU, or workload pressure."
        )
        threshold = "1000-2500 ms"

    else:
        severity = "CRITICAL"
        description = (
            "Severe instance response-time degradation; "
            "investigate system performance immediately."
        )
        threshold = "> 2500 ms"

    return {
        "severity": severity,
        "color": SMLG_COLORS[severity],
        "value_ms": value,
        "threshold": threshold,
        "description": description,
    }


def analyze_smlg(extracted_data: dict[str, Any]) -> dict[str, Any]:
    """
    Analyze structured SMLG evidence.

    Classification is performed independently for every
    SAP instance.
    """

    instances = extracted_data.get("instances", [])

    analyzed_instances = []
    findings = []

    for instance in instances:

        response_time_ms = instance.get("response_time_ms")

        if response_time_ms is None:
            continue

        classification = classify_response_time(
            response_time_ms
        )

        analyzed = {
            **instance,
            "severity": classification["severity"],
            "color": classification["color"],
            "threshold": classification["threshold"],
            "description": classification["description"],
        }

        analyzed_instances.append(analyzed)

        if classification["severity"] == "WARNING":
            findings.append({
                "severity": "WARNING",
                "category": "elevated_instance_response_time",
                "instance": instance.get("instance"),
                "response_time_ms": response_time_ms,
                "threshold": "1000-2500 ms",
                "reason": (
                    f"Instance response time is "
                    f"{response_time_ms} ms."
                ),
            })

        elif classification["severity"] == "CRITICAL":
            findings.append({
                "severity": "CRITICAL",
                "category": "critical_instance_response_time",
                "instance": instance.get("instance"),
                "response_time_ms": response_time_ms,
                "threshold": "> 2500 ms",
                "reason": (
                    f"Instance response time is "
                    f"{response_time_ms} ms."
                ),
            })

    # ---------------------------------------------------------------
    # Overall health
    # ---------------------------------------------------------------

    if not analyzed_instances:
        health = "UNKNOWN"

    elif any(
        item["severity"] == "CRITICAL"
        for item in analyzed_instances
    ):
        health = "CRITICAL"

    elif any(
        item["severity"] == "WARNING"
        for item in analyzed_instances
    ):
        health = "WARNING"

    else:
        health = "HEALTHY"

    response_times = [
        item["response_time_ms"]
        for item in analyzed_instances
    ]

    summary = {
        "instance_count": len(analyzed_instances),
        "healthy_instances": sum(
            1
            for item in analyzed_instances
            if item["severity"] == "HEALTHY"
        ),
        "warning_instances": sum(
            1
            for item in analyzed_instances
            if item["severity"] == "WARNING"
        ),
        "critical_instances": sum(
            1
            for item in analyzed_instances
            if item["severity"] == "CRITICAL"
        ),
    }

    if response_times:
        summary["max_response_time_ms"] = max(response_times)
        summary["avg_response_time_ms"] = (
            sum(response_times) / len(response_times)
        )

    return {
        "health": health,
        "summary": summary,
        "instances": analyzed_instances,
        "findings": findings,
    }
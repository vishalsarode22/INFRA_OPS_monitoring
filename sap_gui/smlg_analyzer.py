"""
SMLG response-time analyzer.

Severity bands, per SAP instance, on SMLG's "Resp.time (ms)" column:

    <= 1500 ms      NORMAL     green
    >  1500 ms      WARNING    yellow
    >  2000 ms      CRITICAL   red
    >  2500 ms      CRITICAL   red, flashing

Thresholds are EXCLUSIVE ("greater than 1500 is yellow"), so exactly
1500 ms is still normal. This differs on purpose from the inclusive
`value >= warning` rule in evaluation/threshold_engine.py: SMLG is graded
here rather than there precisely so it can keep its own rule, and because
the generic engine understands only two thresholds, not three.

Why four bands map onto three Status values
-------------------------------------------
core.models.Status has exactly four members (NORMAL / WARNING / CRITICAL /
UNKNOWN), and much of the pipeline switches on it: the overall-status
rollup, the generic threshold engine, the Excel writers, correlation.
Adding a fifth member to express "critical, and also flashing" would mean
auditing every one of those call sites, and would push a presentation
concern into a domain enum.

So the coarse grade stays in Status and the finer grade travels beside it
as `alert_level`, which is what the dashboard reads to decide whether to
flash. Anything that understands only Status still grades a 2600 ms
response as CRITICAL and behaves correctly; only the visual treatment is
extra.
"""

from __future__ import annotations

import re
from typing import Any

from core.models import Status


# Exclusive lower bounds, in milliseconds.
SMLG_THRESHOLDS = {
    "warning_ms": 1500.0,
    "critical_ms": 2000.0,
    "emergency_ms": 2500.0,
}

# alert_level values. This is the contract with the dashboard -- these exact
# strings are mapped to CSS classes in dashboard/static/shell.js.
ALERT_NORMAL = "normal"
ALERT_WARNING = "warning"
ALERT_CRITICAL = "critical"
ALERT_EMERGENCY = "critical_flashing"

SMLG_COLORS = {
    ALERT_NORMAL: "#34d399",
    ALERT_WARNING: "#fbbf24",
    ALERT_CRITICAL: "#f87171",
    ALERT_EMERGENCY: "#dc2626",
}


def parse_response_ms(raw: Any) -> float | None:
    """
    Parse an SMLG "Resp.time (ms)" reading into milliseconds.

    SMLG renders this column as a whole number of milliseconds carrying a
    locale-dependent THOUSANDS separator: an instance averaging 1277 ms
    shows as "1.277" under a German locale and "1,277" under an English
    one. The previous parse did `float(str(value).replace(",", "."))`,
    which turned "1,277" into 1.277 -- a 1277 ms response recorded as
    roughly one millisecond. That is a thousandfold understatement, and no
    threshold on this metric could ever fire while it was in place.

    Since the column is always an integer count of milliseconds, every
    separator appearing in it is a thousands separator, so all of them are
    stripped rather than guessed at.
    """
    if raw is None or isinstance(raw, bool):
        return None

    # A value that already arrived as a number needs no separator handling.
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return None

    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None

    try:
        return float(digits)
    except ValueError:
        return None


def smlg_limits() -> dict[str, float]:
    """
    The three bands for the system being swept: its override in
    config/thresholds_systems.yaml, else sap.smlg.response_time in
    config/thresholds.yaml, else SMLG_THRESHOLDS above.
    """
    out = dict(SMLG_THRESHOLDS)
    try:
        from core import thresholds as _thr
        spec = _thr.spec("sap.smlg.response_time")
        for field, key in (("warning", "warning_ms"), ("critical", "critical_ms"), ("emergency", "emergency_ms")):
            if spec.get(field) is not None:
                out[key] = float(spec[field])
    except Exception:  # noqa: BLE001 -- a config problem must not stop grading
        pass
    # Emergency is edited in the file only; a system whose critical limit was
    # raised above it must not flash "emergency" below its own critical.
    out["emergency_ms"] = max(out["emergency_ms"], out["critical_ms"])
    out["critical_ms"] = max(out["critical_ms"], out["warning_ms"])
    return out


def _ms(v: float) -> str:
    return f"{int(v)}" if float(v).is_integer() else f"{v:g}"


def classify_response_time(response_time_ms: Any) -> dict[str, Any]:
    """
    Classify one SMLG instance response time.

    Returns both the coarse `status` (a core.models.Status, for the
    monitoring pipeline) and the finer `alert_level` (a string, for the
    dashboard's visual treatment). See the module docstring for why the
    two are separate.
    """
    value = parse_response_ms(response_time_ms)

    if value is None:
        return {
            "status": Status.UNKNOWN,
            "alert_level": ALERT_NORMAL,
            "color": None,
            "value_ms": None,
            "threshold": "",
            "description": "No SMLG response time could be read.",
        }

    lim = smlg_limits()
    if value > lim["emergency_ms"]:
        status = Status.CRITICAL
        alert_level = ALERT_EMERGENCY
        threshold = f"> {_ms(lim['emergency_ms'])} ms"
        description = (
            "Severe instance response-time degradation; investigate "
            "system performance immediately."
        )

    elif value > lim["critical_ms"]:
        status = Status.CRITICAL
        alert_level = ALERT_CRITICAL
        threshold = f"> {_ms(lim['critical_ms'])} ms"
        description = (
            "Instance response time is well above target; check database, "
            "enqueue, CPU and workload pressure now."
        )

    elif value > lim["warning_ms"]:
        status = Status.WARNING
        alert_level = ALERT_WARNING
        threshold = f"> {_ms(lim['warning_ms'])} ms"
        description = (
            "Elevated instance response time; investigate database, "
            "enqueue, CPU, or workload pressure."
        )

    else:
        status = Status.NORMAL
        alert_level = ALERT_NORMAL
        threshold = f"<= {_ms(lim['warning_ms'])} ms"
        description = "Normal instance response time."

    return {
        "status": status,
        "alert_level": alert_level,
        "color": SMLG_COLORS[alert_level],
        "value_ms": value,
        "threshold": threshold,
        "description": description,
    }


# Worst-first, for rolling per-instance grades up to one overall grade.
_ALERT_RANK = {
    ALERT_NORMAL: 0,
    ALERT_WARNING: 1,
    ALERT_CRITICAL: 2,
    ALERT_EMERGENCY: 3,
}


def analyze_smlg(extracted_data: dict[str, Any]) -> dict[str, Any]:
    """
    Analyze structured SMLG evidence.

    Every SAP instance is classified independently, then rolled up to the
    worst grade seen -- one slow instance is a real problem even when the
    group average looks fine, which is the whole reason SMLG is watched
    per instance rather than as a single number.
    """
    instances = extracted_data.get("instances", []) or []

    analyzed_instances: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for instance in instances:
        classification = classify_response_time(
            instance.get("response_time_ms")
        )

        if classification["value_ms"] is None:
            continue

        analyzed = {
            **instance,
            "response_time_ms": classification["value_ms"],
            "status": classification["status"].value,
            "alert_level": classification["alert_level"],
            "color": classification["color"],
            "threshold": classification["threshold"],
            "description": classification["description"],
        }
        analyzed_instances.append(analyzed)

        if classification["alert_level"] != ALERT_NORMAL:
            findings.append({
                "severity": classification["status"].value,
                "alert_level": classification["alert_level"],
                "category": "instance_response_time",
                "instance": instance.get("instance"),
                "response_time_ms": classification["value_ms"],
                "threshold": classification["threshold"],
                "reason": (
                    f"Instance response time is "
                    f"{classification['value_ms']:.0f} ms "
                    f"({classification['threshold']})."
                ),
            })

    if not analyzed_instances:
        health = Status.UNKNOWN.value
        worst_alert = ALERT_NORMAL
    else:
        worst = max(
            analyzed_instances,
            key=lambda item: _ALERT_RANK[item["alert_level"]],
        )
        worst_alert = worst["alert_level"]
        health = worst["status"]

    response_times = [item["response_time_ms"] for item in analyzed_instances]

    summary = {
        "instance_count": len(analyzed_instances),
        "normal_instances": sum(
            1 for i in analyzed_instances if i["alert_level"] == ALERT_NORMAL),
        "warning_instances": sum(
            1 for i in analyzed_instances if i["alert_level"] == ALERT_WARNING),
        "critical_instances": sum(
            1 for i in analyzed_instances
            if i["alert_level"] in (ALERT_CRITICAL, ALERT_EMERGENCY)),
        "flashing_instances": sum(
            1 for i in analyzed_instances if i["alert_level"] == ALERT_EMERGENCY),
    }

    if response_times:
        summary["max_response_time_ms"] = max(response_times)
        summary["avg_response_time_ms"] = sum(response_times) / len(response_times)

    return {
        "health": health,
        "alert_level": worst_alert,
        "summary": summary,
        "instances": analyzed_instances,
        "findings": findings,
    }

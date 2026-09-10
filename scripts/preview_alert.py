"""
Preview the diagnostic alert against a live system, without waiting for an
incident to fire.

    python scripts/preview_alert.py PRD

Writes alert_<SYSTEM>.html in the working directory and opens it. Read-only:
one RFC read, no sweep, nothing written to SAP and no email sent.
"""

from __future__ import annotations

import os
import sys
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.rfc_live import read_live                 # noqa: E402
from core.config_loader import get_systems                # noqa: E402
from core.models import MetricResult, MonitoringResult, Status  # noqa: E402
from notifications.diagnostic_alert import build_diagnostic_alert  # noqa: E402


# Metrics whose thresholds decide the headline severity. Everything else is
# reported but does not drive the banner.
_THRESHOLDS = {
    "cpu": (85, 95),
    "memory": (85, 95),
    "load_1m": (8, 16),
    "dialog_queue": (5, 20),
    "update_queue": (5, 20),
}

# 0 is a real reading for a queue or a load average, but never for CPU or
# memory -- a live host is not at 0% CPU. Matches the collector's own rule.
_ZERO_IS_UNKNOWN = {"cpu", "memory"}


def _status_for(name: str, value) -> Status:
    if value is None:
        return Status.UNKNOWN
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return Status.NORMAL
    if name in _ZERO_IS_UNKNOWN and value == 0:
        return Status.UNKNOWN
    warn, crit = _THRESHOLDS.get(name, (None, None))
    if crit is not None and value >= crit:
        return Status.CRITICAL
    if warn is not None and value >= warn:
        return Status.WARNING
    return Status.NORMAL


def _metrics_from(payload: dict) -> list[MetricResult]:
    metrics: list[MetricResult] = []

    def add(name, value, source, detail="", unit=""):
        warn, crit = _THRESHOLDS.get(name, (None, None))
        display = "—" if value is None else f"{value}{unit}"
        metrics.append(MetricResult(
            name=name,
            value=value if isinstance(value, (int, float)) and not isinstance(value, bool) else None,
            display_value=str(display),
            status=_status_for(name, value),
            threshold_warning=warn,
            threshold_critical=crit,
            source=source,
            detail=detail,
        ))

    smon = payload.get("smon") or {}
    add("cpu", payload.get("cpu"), "RFC · SMON", unit="%")
    add("memory", payload.get("memory"), "RFC · SMON",
        detail=(f"{smon.get('free_mem_mb')} MB free"
                if smon.get("free_mem_mb") is not None else ""), unit="%")
    add("load_1m", payload.get("load_1m"), "RFC · SMON CPU_CONS",
        detail=(f"{smon.get('cpus')} CPUs" if smon.get("cpus") else ""))

    for key, label in (("dialog_queue", "dialog queue"),
                       ("update_queue", "update queue"),
                       ("sessions", "sessions"),
                       ("db_rtt_ms", "DB round-trip ms")):
        if key in smon:
            add(key, smon.get(key), "RFC · SMON", detail=label)

    for check in payload.get("checks") or []:
        add(check.get("metric") or check.get("tcode") or "check",
            check.get("value"),
            f"RFC · {payload.get('check_source') or 'Z_FM'}",
            detail=f"{check.get('label','')} ({check.get('tcode','')})".strip())

    icm = payload.get("icm") or {}
    add("icm", None if icm.get("label") in (None, "Unknown") else 1,
        icm.get("source") or "sapcontrol · SSH",
        detail=icm.get("label") or "not read")

    if not payload.get("connected", True):
        metrics.append(MetricResult(
            name="rfc.connection", value=None, display_value="—",
            status=Status.UNKNOWN, source="RFC",
            detail=str(payload.get("error") or "not connected")))

    return metrics


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "PRD"

    systems = {s["name"]: s for s in get_systems()}
    if name not in systems:
        print(f"System '{name}' not in systems.yaml. Known: {', '.join(sorted(systems))}")
        return 1

    cfg = systems[name]
    payload = read_live(name, cfg, use_cache=False)
    metrics = _metrics_from(payload)

    result = MonitoringResult(
        system=name,
        client=str(cfg.get("client") or ""),
        metrics=metrics,
        errors=[payload["error"]] if payload.get("error") else [],
    )
    result.overall_status = result.compute_overall_status()

    # No incident is passed: this is a preview of current state, not a
    # correlated incident. The alert says so rather than inventing one.
    subject, body = build_diagnostic_alert(result)

    out = os.path.abspath(f"alert_{name}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(body)

    print(subject)
    print(f"{len(metrics)} metrics · "
          f"{sum(1 for m in metrics if m.status is Status.UNKNOWN)} unreadable")
    print(out)
    webbrowser.open(f"file:///{out.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

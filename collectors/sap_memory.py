"""
SAP-level resource metrics that do NOT depend on saposcol.

Why this exists
---------------
CPU, physical memory and load average are OS facts. Every RFC route to them
-- the Z observability FM, /SDF/SMON, and the CCMS RZ20 monitor -- ultimately
reads saposcol. On a host where saposcol is not running (confirmed on
CENTOR_QAS: the RZ20 Operating System nodes exist but every value reads 0
with a blank ALRELVALDT, meaning nothing has ever written to them), those
figures genuinely do not exist to be read. No amount of RFC cleverness
invents them; the fix there is `startsap` for saposcol, on the host.

What CAN be read without saposcol is the ABAP server's own view of itself,
because the kernel tracks it internally and reports it over RFC:

  * SAP memory      extended memory / heap / roll in use vs configured
                    (ST02). This is the number that actually predicts a
                    PRIV-mode cascade, which is what a Basis person cares
                    about -- arguably more actionable than host RAM.
  * CPU time        accumulated CPU seconds across work processes
                    (TH_WPINFO). Not a utilisation percentage -- see the
                    honest naming below -- but it shows load moving.

These are deliberately NOT named sap.os.cpu / sap.os.memory. They measure a
different thing from host CPU/RAM and labelling them as if they were the
same would be the exact "make a number appear" mistake this project avoids.
A tile fed from here says SAP memory, not memory.
"""

from __future__ import annotations

from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__)

# ST02 memory areas worth reporting. Key = ST02 area name in the FM output.
_AREAS = {
    "EXTM": ("Extended memory", "sap.st02.extended_memory_pct"),
    "HEAP": ("Heap memory", "sap.st02.heap_memory_pct"),
    "ROLL": ("Roll area", "sap.st02.roll_area_pct"),
}


def _num(raw):
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _metric(name, value, unit, tcode, detail, status=None, extra=None):
    return MetricResult(
        name=name, value=float(value),
        display_value=f"{value:.1f}{unit}" if unit == "%" else f"{value:.0f} {unit}",
        status=status or (Status.CRITICAL if value >= 90 else
                          Status.WARNING if value >= 75 else Status.NORMAL),
        threshold_warning=75, threshold_critical=90,
        source="rfc_sapmem", tcode=tcode, detail=detail, category="resource",
        unit=unit, extra_data={"collector": "RFC_SAPMEM", **(extra or {})})


def collect(session, system: str) -> list[MetricResult]:
    """
    SAP-internal memory utilisation. Never raises; returns [] when the
    function module is not available (older kernels, or no S_RFC for it).
    """
    if not getattr(session, "ok", False):
        return []

    out: list[MetricResult] = []
    got = session.call("SAPTUNE_GET_SUMMARY_STATISTIC")
    if not got:
        log.info(f"[{system}] SAPTUNE_GET_SUMMARY_STATISTIC not callable -- "
                 "SAP memory metrics unavailable (needs S_RFC for the FM)")
        return []

    rows = got.get("STORAGE") or got.get("ES_STORAGE") or []
    if isinstance(rows, dict):
        rows = [rows]

    for row in rows:
        area = str(row.get("AREA", "") or row.get("NAME", "")).strip().upper()
        if area not in _AREAS:
            continue
        label, metric_name = _AREAS[area]

        used = _num(row.get("USED")) or _num(row.get("CURUSE"))
        total = _num(row.get("SIZE")) or _num(row.get("MAXUSE")) or _num(row.get("TOTAL"))
        if used is None or not total:
            continue
        pct = round(100.0 * used / total, 1)
        if not (0 <= pct <= 100):
            continue

        out.append(_metric(
            metric_name, pct, "%", "ST02",
            detail=f"{label}: {used:.0f} of {total:.0f} in use",
            extra={"used": used, "total": total, "area": area}))

    if out:
        log.info(f"[{system}] SAP memory collector: {len(out)} metrics")
    return out


def cpu_from_work_processes(wp_rows: list[dict], system: str) -> MetricResult | None:
    """
    Share of work processes currently busy.

    This is NOT host CPU utilisation and is not presented as such. It is the
    dispatcher's own view of how much of its capacity is committed right now,
    which is the figure that matters when asking "is this instance saturated"
    -- and unlike ST06 it needs nothing but TH_WPINFO, which already answers
    on every system here.
    """
    rows = [r for r in (wp_rows or []) if str(r.get("WP_TYP", "")).strip()]
    if not rows:
        return None
    busy = sum(1 for r in rows
               if not str(r.get("WP_STATUS", "")).strip().lower().startswith("wait"))
    pct = round(100.0 * busy / len(rows), 1)
    return MetricResult(
        name="sap.sm50.wp_busy_pct", value=float(pct), display_value=f"{pct:.1f}%",
        status=Status.CRITICAL if pct >= 90 else Status.WARNING if pct >= 75 else Status.NORMAL,
        threshold_warning=75, threshold_critical=90,
        source="rfc_sapmem", tcode="SM50",
        detail=f"{busy} of {len(rows)} work processes busy (dispatcher view, not host CPU)",
        category="resource", unit="%",
        extra_data={"collector": "RFC_SAPMEM", "busy": busy, "total": len(rows)})

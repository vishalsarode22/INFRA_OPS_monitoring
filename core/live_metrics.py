"""
Turn one live RFC read into a MonitoringResult.

This existed in three places before -- scripts/preview_alert.py, the on-demand
RCA endpoint, and the live health report -- with the thresholds and the
zero-handling rules copied into each. Three copies of a threshold table means
three chances for them to drift, and a metric graded WARNING on one screen and
NORMAL on another destroys trust in both.

The zero rules are the part most easily got wrong, so they live here once:

    0% CPU or 0% memory  -> UNKNOWN. A live application server is never at
                            exactly 0%; that reading means the OS collector
                            fed SMON nothing this cycle.

    0 on a queue or load -> NORMAL. An idle system genuinely has an empty
                            dialog queue and a load average of 0.00.

Thresholds are defaults, not authority. The Excel Configuration sheet is the
threshold authority for this project; pass `overrides` to use it once the
sheet is wired in, rather than editing the numbers here.
"""

from __future__ import annotations

from core.models import MetricResult, MonitoringResult, Status


# (warning, critical). None means the metric is reported but not graded.
DEFAULT_THRESHOLDS: dict[str, tuple[float | None, float | None]] = {
    "cpu": (85, 95),
    "memory": (85, 95),
    "load_1m": (8, 16),
    "dialog_queue": (5, 20),
    "update_queue": (5, 20),
    "db_rtt_ms": (50, 200),
    # T-code counters. A dump is not automatically a problem; a spike is.
    "sap.st22.dumps": (5, 25),
    "sap.sm12.lock_count": (10, 50),
    "sap.sm13.failed_updates": (1, 10),
    "sap.sm37.cancelled_jobs": (1, 5),
    "sap.sm58.stuck_trfc": (5, 25),
    "sap.smq1.stuck_queues": (1, 10),
    "sap.smq2.stuck_queues": (1, 10),
    "sap.we02.failed_idocs": (10, 100),
    "sap.su01.locked_users": (100, 300),
    # BALHDR is the APPLICATION log (SLG1), not SM21's system log. The metric
    # was named sap.sm21.errors, which asserted a source it never read.
    # Renaming breaks history for the old key -- done once, deliberately.
    "sap.slg1.app_log_errors": (10, 50),
    "sap.sm21.errors": (10, 50),   # legacy key, kept so old snapshots still grade
}

# Metrics where zero means "not measured", not "measured as zero".
ZERO_IS_UNKNOWN = {"cpu", "memory", "memory.total_gb"}

# Metrics where a SMALLER number is worse (availability percentages, hit
# ratios). Grading everything as higher-is-worse is how an availability of
# 100% once graded as RED_ALERT in the previous codebase.
LOWER_IS_WORSE: set[str] = set()

_EXCEL_CACHE: dict | None = None


def excel_thresholds() -> dict[str, tuple[float | None, float | None]]:
    """
    Thresholds from the Excel template's Configuration sheet.

    That sheet is this project's threshold authority. DEFAULT_THRESHOLDS below
    exists only so a missing or unreadable template degrades to something
    sensible rather than grading nothing -- two maintained sets of numbers
    would drift, and a metric graded WARNING on one screen and NORMAL on
    another destroys trust in both.

    Never raises: a broken template must not take the dashboard down.
    """
    global _EXCEL_CACHE
    if _EXCEL_CACHE is not None:
        return _EXCEL_CACHE

    merged = dict(DEFAULT_THRESHOLDS)
    try:
        from reporting.infrabeatops_template_writer import load_thresholds
        for metric, spec in (load_thresholds() or {}).items():
            warn = spec.get("warning")
            crit = spec.get("critical")
            merged[metric] = (
                float(warn) if warn is not None else None,
                float(crit) if crit is not None else None,
            )
            if str(spec.get("direction", "")).upper() == "LOWER":
                LOWER_IS_WORSE.add(metric)
    except Exception:
        pass

    _EXCEL_CACHE = merged
    return merged


def status_for(name: str, value, thresholds=None) -> Status:
    thresholds = thresholds if thresholds is not None else excel_thresholds()
    if value is None:
        return Status.UNKNOWN
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Status.NORMAL
    if name in ZERO_IS_UNKNOWN and value == 0:
        return Status.UNKNOWN
    warn, crit = thresholds.get(name, (None, None))

    if name in LOWER_IS_WORSE:
        if crit is not None and value <= crit:
            return Status.CRITICAL
        if warn is not None and value <= warn:
            return Status.WARNING
        return Status.NORMAL

    if crit is not None and value >= crit:
        return Status.CRITICAL
    if warn is not None and value >= warn:
        return Status.WARNING
    return Status.NORMAL


def metrics_from_live(payload: dict, thresholds=None) -> list[MetricResult]:
    """Every metric the live RFC read produced, graded."""
    thresholds = thresholds if thresholds is not None else excel_thresholds()
    metrics: list[MetricResult] = []
    smon = payload.get("smon") or {}

    def add(name, value, source, detail="", unit="", tcode=None, category=""):
        warn, crit = thresholds.get(name, (None, None))
        metrics.append(MetricResult(
            name=name,
            value=value if isinstance(value, (int, float))
            and not isinstance(value, bool) else None,
            display_value="—" if value is None else f"{value}{unit}",
            status=status_for(name, value, thresholds),
            threshold_warning=warn,
            threshold_critical=crit,
            source=source,
            tcode=tcode,
            detail=detail,
            category=category,
        ))

    # ---- host metrics ----------------------------------------------------
    #
    # The source is NOT always SMON. read_live fills cpu/memory/load from
    # whichever of four sources answered -- the Z FM, /SDF/SMON, CCMS RZ20 or
    # ST02 -- and records which in payload["fallback_sources"]. Hardcoding
    # "RFC · SMON" here labelled a CCMS reading as SMON and, worse, on a
    # system where SMON gave nothing it presented the fallback figure as
    # though SMON had produced it. Read the real source so the panel and the
    # wall agree on where a number came from.
    fb = payload.get("fallback_sources") or {}

    def host_source(field: str, default: str) -> str:
        src = (fb.get(field) or {}).get("source")
        return src or default

    cpu_val = payload.get("cpu")
    mem_val = payload.get("memory")
    add("cpu", cpu_val, host_source("cpu", "RFC · SMON"), unit="%", category="host",
        detail=f"idle {smon.get('cpu_idle_raw')}%"
        if smon.get("cpu_idle_raw") is not None else "")

    mem_detail = ""
    if payload.get("memory_is_sap_internal"):
        mem_detail = "SAP extended memory (ST02), not host RAM"
    elif smon.get("memory_basis"):
        mem_detail = smon["memory_basis"]
    elif smon.get("free_mem_mb") is not None:
        mem_detail = f"{smon.get('free_mem_mb')} MB free"
    elif (payload.get("ccms") or {}).get("mem_free_display"):
        mem_detail = payload["ccms"]["mem_free_display"]
    add("memory", mem_val, host_source("memory", "RFC · SMON"), unit="%",
        category="host", detail=mem_detail)
    add("load_1m", payload.get("load_1m"),
        host_source("load_1m", "RFC · SMON CPU_CONS"), category="host",
        detail=f"{smon.get('cpus')} CPUs" if smon.get("cpus") else "")

    for key, label in (("dialog_queue", "dialog queue"),
                       ("update_queue", "update queue"),
                       ("sessions", "sessions"),
                       ("db_rtt_ms", "DB round-trip")):
        if key in smon:
            add(key, smon.get(key), "RFC · SMON", detail=label, category="workload",
                unit=" ms" if key == "db_rtt_ms" else "")

    # ---- T-code counters, from Z_GET_OBSERVABILITY_DATA -------------------
    source = f"RFC · {payload.get('check_source') or 'Z_FM'}"
    for check in payload.get("checks") or []:
        name = check.get("metric") or check.get("tcode") or "check"
        add(name, check.get("value"), source,
            detail=check.get("detail") or check.get("label") or "",
            tcode=check.get("tcode"), category="sap")

    # ---- process and service state ---------------------------------------
    wp = payload.get("work_processes") or {}
    if wp.get("total"):
        add("sap.sm50.wp_in_use", wp.get("in_use"), "RFC · TH_WPINFO",
            detail=f"{wp.get('in_use')} of {wp.get('total')} in use", category="sap")

    # Dispatcher and ICM come back from TH_WPINFO / ICM_GET_INFO2 over RFC.
    # The gateway has no equivalent RFC read here, so sap.gateway.state was
    # always UNKNOWN -- a permanently unreadable row that added nothing to
    # the findings and crowded out real ones. It is dropped rather than
    # reported as an eternal blind spot. (sapcontrol on the host does expose
    # it; if that path is ever wired up, re-add "gateway" to this tuple.)
    for key, label in (("dispatcher", "Dispatcher"), ("icm", "ICM")):
        state = payload.get(key) or {}
        if not state:
            continue
        text = state.get("label")
        metrics.append(MetricResult(
            name=f"sap.{key}.state", value=None,
            display_value=str(text or "Unknown"),
            # An unreadable service state is UNKNOWN, never NORMAL. This is
            # the rule the whole project exists to enforce.
            status=Status.NORMAL if str(text).lower() == "running"
            else Status.UNKNOWN if text in (None, "", "Unknown")
            else Status.CRITICAL,
            source=state.get("source") or "RFC", detail=label, category="sap"))

    # ---- reachability ----------------------------------------------------
    if not payload.get("connected", True):
        metrics.append(MetricResult(
            name="rfc.connection", value=None, display_value="not connected",
            status=Status.UNKNOWN, source="RFC", category="host",
            detail=str(payload.get("error") or "no response")))

    return metrics


def build_live_result(system: str, cfg: dict, thresholds=None,
                      use_cache: bool = False) -> tuple[MonitoringResult, dict]:
    """
    One live RFC read turned into a graded MonitoringResult.

    Returns (result, raw_payload). The raw payload is returned as well so
    callers can report the connection error verbatim -- summarising an RFC
    error loses the part that identifies the failure class.
    """
    from collectors.rfc_live import read_live

    payload = read_live(system, cfg, use_cache=use_cache)

    result = MonitoringResult(system=system, client=str(cfg.get("client") or ""))
    result.metrics = metrics_from_live(payload, thresholds)
    if payload.get("error"):
        result.errors.append(str(payload["error"]))
    result.overall_status = result.compute_overall_status()

    # Record the sample. Throttled to one per metric per 5 minutes inside the
    # store, so the wall's 10-second poll does not write 8,600 points a day
    # to describe a value that moves over weeks. Failure here is swallowed:
    # losing a history sample must never take a monitoring read down.
    try:
        from core.metric_history import append_metrics
        append_metrics(system, result.metrics)
    except Exception:
        pass

    return result, payload

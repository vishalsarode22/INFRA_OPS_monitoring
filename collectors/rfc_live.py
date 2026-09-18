"""
Live RFC read for the operations wall display.

WHY THIS IS SEPARATE FROM THE SWEEP
-----------------------------------
The monitoring sweep is heavy and slow: it drives SAP GUI, captures
screenshots, runs OCR, writes evidence, builds reports and calls the AI
layer. It is meant to run on a dedicated monitoring machine every few hours.

A wall display needs the opposite: a small, fast, read-only snapshot every
few seconds, with no side effects. Running the sweep to refresh a screen
would relaunch SAP Logon and kill the operator's own GUI session.

So this module does one thing: open one RFC connection, read the handful of
values the wall display shows, close it. It writes nothing, triggers no
alerts, creates no events and produces no evidence.

WHAT IT READS
-------------
    instances        TH_SERVER_LIST      name, host, type per app server
    work processes   TH_WPINFO           total / in use / free, per type
    dispatcher, ICM  sapcontrol via Z FM or SXPG
    CPU / memory     Z_GET_OBSERVABILITY_DATA (SXPG -> /proc)
    response time    NOT available over RFC -- see below

RESPONSE TIME
-------------
Per-instance dialog response time (what SMLG shows) lives in the ST03
workload statistics, which need the SAP_COLLECTOR_FOR_PERFMONITOR job and
the SAPWL_* function modules. That interface was not confirmed on this
release, so this module does NOT invent a number for it.

Instead `response_time` is read from the most recent monitoring snapshot,
where the SAP GUI collector stores `sap.smlg.response_time`, and is returned
WITH ITS AGE. A stale number labelled stale is useful; a stale number
presented as live is not.
"""

from __future__ import annotations

import os as _os
import threading
import time
from datetime import datetime, timedelta

from collectors.rfc_collector import (
    SapSession,
    cooldown_remaining,
    clear_cooldown,
    park,
    _from_function_module,
    _from_standard_modules,
)
from collectors.rfc_perf import build_metrics as _perf_build_metrics
from collectors import rfc_perf as _rp
from collectors import ccms_os as _ccms
from utils.logger import get_logger

log = get_logger(__name__)

# One live read per system is cached briefly. A wall display polling every
# 5s across 4 systems would otherwise open 48 RFC logons a minute, which is
# the audit-log and account-lockout problem the sweep already had to solve.
_CACHE_TTL_SECONDS = int(_os.environ.get("IBO_LIVE_TTL_SECONDS", "60") or 60)
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

# Reads that are expensive AND slow-moving get their own, longer clock.
# Dump breakdowns, background-job detail and locked-user lists are ~15 RFC
# round trips between them and do not change meaningfully inside a minute;
# re-reading them on every pass was most of the per-poll cost. The previous
# payload is carried forward between deep passes, labelled with its age, so
# the UI never blanks a panel just because this pass skipped it.
_DEEP_TTL_SECONDS = int(_os.environ.get("IBO_LIVE_DEEP_TTL_SECONDS", "300") or 300)
_deep_cache: dict[str, tuple[float, dict]] = {}
_deep_lock = threading.Lock()


def _deep_cached(system: str) -> dict | None:
    with _deep_lock:
        hit = _deep_cache.get(system)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age > _DEEP_TTL_SECONDS:
        return None
    out = dict(hit[1])
    out["_age_seconds"] = round(age, 1)
    return out


def _deep_store(system: str, payload: dict) -> None:
    with _deep_lock:
        _deep_cache[system] = (time.monotonic(), payload)


def reset_deep_cache(system: str | None = None) -> None:
    with _deep_lock:
        _deep_cache.clear() if system is None else _deep_cache.pop(system, None)


def _cached(system: str):
    with _lock:
        hit = _cache.get(system)
    if not hit:
        return None
    age, payload = time.monotonic() - hit[0], hit[1]
    if age > _CACHE_TTL_SECONDS:
        return None
    out = dict(payload)
    out["cache_age_seconds"] = round(age, 1)
    return out


def _store(system: str, payload: dict):
    with _lock:
        _cache[system] = (time.monotonic(), payload)


# Human labels for the T-code counters, so the wall reads like a Basis
# checklist rather than a list of metric keys.
_CHECK_LABELS = {
    "sap.st22.dumps":          ("ST22",  "ABAP dumps today"),
    "sap.sm12.lock_count":     ("SM12",  "Lock entries"),
    "sap.sm13.failed_updates": ("SM13",  "Failed updates"),
    "sap.sm37.cancelled_jobs": ("SM37",  "Cancelled jobs"),
    "sap.sm37.active_jobs":    ("SM37",  "Running jobs"),
    "sap.sm58.stuck_entries":  ("SM58",  "tRFC"),
    "sap.smq1.entries":        ("SMQ1",  "Outbound queues"),
    "sap.smq2.entries":        ("SMQ2",  "Inbound queues"),
    "sap.we02.failed_idocs":   ("WE02",  "Failed IDocs"),
    "sap.sm21.errors":         ("SM21",  "Log errors"),
    "sap.al08.user_logons":    ("AL08",  "User sessions"),
    "sap.su01.locked_users":   ("SU01",  "Locked users"),
    "sap.sm50.free_dia_wp":    ("SM50",  "Free dialog WP"),
    "sap.sm50.total_dia_wp":   ("SM50",  "Total dialog WP"),
    "sap.sm66.wp_saturation_pct": ("SM66", "WP saturation"),
    "sap.db12.last_backup":    ("DB12",  "Last backup"),
    # rfc_perf collector -- same session, cached longer (see _PERF_TTL_SECONDS)
    "sap.sm50.priv_mode_wp":            ("SM50", "WPs in PRIV mode"),
    "sap.sm50.long_running_wp":         ("SM50", "Long-running WPs"),
    "sap.sm66.max_instance_saturation_pct": ("SM66", "Busiest instance"),
    "sap.st03.dialog_resp_ms":          ("ST03", "Dialog response"),
    "sap.st03.max_instance_resp_ms":    ("ST03", "Slowest instance"),
    "sap.st03.db_time_pct":             ("ST03", "DB share of response"),
    "sap.st03.top_report_db_ms":        ("ST03", "Top report · total DB time"),
    "sap.st03.top_user_memory_mb":      ("ST03", "Top user by memory"),
    "sap.sm12.locks_per_user_max":      ("SM12", "Most locks per user"),
    "sap.sm12.users_with_many_locks":   ("SM12", "Users with many locks"),
    "sap.sm12.oldest_lock_minutes":     ("SM12", "Oldest lock age"),
    "sap.sqlm.expensive_programs":      ("SQLM", "Expensive programs"),
    "sap.sqlm.top_program_total_s":     ("SQLM", "Top program SQL time"),
}

# Shown as tiles at the top of a card; excluded from the checklist grid so
# nothing appears twice.
_TILE_METRICS = {"cpu", "memory", "memory.total_gb", "load_1m",
                 "sap.sm50.free_dia_wp", "sap.sm50.total_dia_wp"}


def _human_ms(value_ms) -> str | None:
    """
    A raw millisecond figure as a readable duration.

    ST03 response and DB-time counters come back as raw ms and were rendered
    "61455 ms" / "13949264 ms" on the wall -- numbers nobody reads at a
    glance and, at that size, ones that invite the "surely that's a unit bug"
    reaction. Past a second show seconds; past a minute, m/s. The exact ms
    stays available in the tooltip via the detail line.
    """
    try:
        ms = float(value_ms)
    except (TypeError, ValueError):
        return None
    if ms < 1000:
        return f"{round(ms)} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    total_s = int(round(ms / 1000))
    if total_s < 3600:
        m, s = divmod(total_s, 60)
        return f"{m}m {s}s"
    h, rem = divmod(total_s, 3600)
    m = rem // 60
    return f"{h}h {m}m"


# ST03 counters whose value is a raw millisecond figure. These get the
# human-duration treatment; everything else keeps its own display value.
_MS_VALUED_CHECKS = {
    "sap.st03.max_instance_resp_ms",
    "sap.st03.top_report_db_ms",
}

# Checks that should NOT appear in the compact grid. Dialog response is shown
# per instance in the instances table already, where it carries the statistic
# label and the readable duration; repeating it here as a bare ms figure was
# both redundant and the least readable tile on the wall.
_CHECK_GRID_EXCLUDE = {
    "sap.st03.dialog_resp_ms",
    "sap.st03.top_report_db_ms",
    "sap.sm12.oldest_lock_minutes",
    "sap.sm66.max_instance_saturation_pct",
    "sap.st03.max_instance_resp_ms",
    "sap.su01.locked_users",
    # Dropped from the wall grid on request (16 Sep 2026). All are still
    # collected, graded and written to rfc_metrics and the reports.
    "sap.sm12.lock_count",
    "sap.sm66.wp_saturation_pct",
    "sap.sm58.stuck_entries",
    "sap.st03.db_time_pct",
    "sap.sm12.locks_per_user_max",
    "sap.sm12.users_with_many_locks",
    "sap.db12.last_backup",
}


def _checks_from_metrics(metrics) -> list[dict]:
    """
    Converts the RFC collector's MetricResult list into compact rows for the
    wall.

    Reuses the collector rather than re-querying, so the live view and the
    sweep grade identically -- two code paths reading the same counter with
    different thresholds is how a dashboard starts disagreeing with its own
    reports.
    """
    rows = []
    for m in metrics:
        if m.name in _TILE_METRICS:
            continue
        if m.name in _CHECK_GRID_EXCLUDE:
            continue
        tcode, label = _CHECK_LABELS.get(m.name, (m.tcode or "—", m.name))
        value = m.display_value
        if m.name in _MS_VALUED_CHECKS:
            human = _human_ms(m.value)
            if human is not None:
                value = human
        rows.append({
            "metric": m.name,
            "tcode": tcode,
            "label": label,
            "value": value,
            "status": m.status.value,
            "detail": (m.detail or "")[:160],
        })
    rows.sort(key=lambda r: ({"CRITICAL": 0, "WARNING": 1, "NORMAL": 2,
                              "UNKNOWN": 3}.get(r["status"], 4), r["tcode"]))
    return rows


def _jsonable(value):
    """extra_data straight to the browser. Datetimes and sets are the only
    things the collectors put in there that json.dumps refuses."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _serialize_metrics(metrics, read_at: str, age_seconds: float = 0) -> list[dict]:
    """
    Every field of every RFC MetricResult, as the dashboard receives it.

    `_checks_from_metrics` is the compact checklist; this is the complete
    record -- thresholds, category, unit, and the collector's extra_data
    (per-instance figures, top users, lock owners). The sweep already
    persists exactly this to the snapshot; the live view should not show
    less than the report does.
    """
    rows = []
    for m in metrics or []:
        tcode, label = _CHECK_LABELS.get(m.name, (m.tcode or "—", m.name))
        extra = dict(m.extra_data or {})
        collector = extra.pop("collector", None) or (m.source or "").upper() or "RFC"
        rows.append({
            "metric": m.name,
            "label": label,
            "tcode": tcode,
            "value": m.value,
            "display_value": m.display_value,
            "unit": m.unit or "",
            "status": m.status.value,
            "threshold_warning": m.threshold_warning,
            "threshold_critical": m.threshold_critical,
            "category": m.category or "",
            "detail": m.detail or "",
            "collector": collector,
            "extra": _jsonable(extra),
            "read_at": read_at,
            "age_seconds": round(age_seconds, 1),
        })
    return rows


# The perf read (rfc_perf.build_metrics) costs 2-4s on PRD -- the STAT
# table scan and the SQLM query dominate, and both were running on nearly
# every refresh pass because this TTL was 60s against a 60s cadence. A
# dialog-response average over a 2-5 minute window, and an "expensive SQL
# today" list, do not change meaningfully inside five minutes; re-reading
# them every minute was most of what pushed a full pass past its ceiling.
# It rides the same RFC session as the fast read (no extra logon) but is
# refreshed on its own, longer clock. `perf.age_seconds` says how old.
_PERF_TTL_SECONDS = int(_os.environ.get("IBO_PERF_TTL_SECONDS", "300") or 300)
_perf_cache: dict[str, tuple[float, list, str | None, str]] = {}
_perf_lock = threading.Lock()


def reset_perf_cache(system: str | None = None) -> None:
    with _perf_lock:
        if system is None:
            _perf_cache.clear()
        else:
            _perf_cache.pop(system, None)
    # A manual refresh should re-check everything that is otherwise learned
    # once per process: the deep detail read and the "SQLM is off here"
    # decision. Without this, activating SQL Monitor and clicking refresh
    # would still skip the scan until the next restart.
    reset_deep_cache(system)
    try:
        _rp.reset_sqlm_off(system)
    except AttributeError:
        pass


# Stale-while-revalidate for the perf block. When the 5-minute cache has
# expired, the caller used to block for the whole 30-40s STAT/SQLM read.
# Now: an expired-but-present result is returned at once (its age is
# reported in perf.age_seconds as before) and ONE background thread refreshes
# it on its own RFC session. Only the very first read of a system after
# startup is synchronous, because there is nothing to serve yet.
_perf_refreshing: set = set()
_PERF_SWR = (_os.environ.get("IBO_PERF_STALE_WHILE_REVALIDATE", "1") or "1").strip().lower() in ("1", "true", "yes")


def _perf_refresh_background(system: str, client: str) -> None:
    try:
        from core.config_loader import get_systems
        cfg = next((c for c in get_systems() if c.get("name") == system), None)
        if cfg is None:
            return
        with SapSession(system, cfg) as bg:
            if not bg.ok:
                log.info(f"[{system}] perf background refresh skipped: {bg.error}")
                return
            read_at = datetime.now().strftime("%H:%M:%S")
            metrics, error = [], None
            try:
                metrics = _perf_build_metrics(system, bg, client) or []
                if not metrics:
                    error = "connected, but no performance source was readable"
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                log.warning(f"[{system}] live perf background read failed: {error}")
            with _perf_lock:
                _perf_cache[system] = (time.monotonic(), metrics, error, read_at)
            log.info(f"[{system}] perf block refreshed in background ({len(metrics)} metrics)")
    finally:
        with _perf_lock:
            _perf_refreshing.discard(system)


def _perf_metrics(session: SapSession, system: str, client: str) -> tuple[list, str | None, float, str]:
    """(MetricResult list, error, age_seconds, read_at). Never raises."""
    with _perf_lock:
        hit = _perf_cache.get(system)
        already = system in _perf_refreshing
    if hit:
        age = time.monotonic() - hit[0]
        if age <= _PERF_TTL_SECONDS:
            return hit[1], hit[2], age, hit[3]
        if _PERF_SWR and hit[1]:
            # Expired but usable: serve it now, refresh it behind the caller.
            if not already:
                with _perf_lock:
                    _perf_refreshing.add(system)
                threading.Thread(target=_perf_refresh_background, args=(system, client),
                                 name=f"perf-refresh-{system}", daemon=True).start()
            return hit[1], hit[2], age, hit[3]

    read_at = datetime.now().strftime("%H:%M:%S")
    metrics, error = [], None
    try:
        metrics = _perf_build_metrics(system, session, client) or []
        if not metrics:
            error = "connected, but no performance source was readable"
    except Exception as exc:  # noqa: BLE001 -- one bad table must not blank the tile
        error = f"{type(exc).__name__}: {exc}"
        log.warning(f"[{system}] live perf read failed: {error}")

    with _perf_lock:
        _perf_cache[system] = (time.monotonic(), metrics, error, read_at)
    return metrics, error, 0.0, read_at


def _process_state(raw) -> tuple[str, str]:
    """Maps a sapcontrol colour to (label, status)."""
    text = str(raw or "").strip().upper()
    if text in ("GREEN", "RUNNING", "OK"):
        return "Running", "NORMAL"
    if text in ("YELLOW", "STARTING", "STOPPING"):
        return text.title(), "WARNING"
    if text in ("RED", "STOPPED", "GRAY"):
        return text.title(), "CRITICAL"
    return "Unknown", "UNKNOWN"


# --------------------------------------------------------------------------
# /SDF/SMON_HEADER -- OS metrics without S_LOG_COM
# --------------------------------------------------------------------------
#
# Z_GET_OBSERVABILITY_DATA deliberately returns -1 for CPU, memory, RAM and
# load. It does not measure them: doing so needed SXPG_COMMAND_EXECUTE and
# therefore S_LOG_COM, which is remote command execution on the application
# server -- the only write-capable grant the monitoring required.
#
# They come from here instead: a table read needing nothing beyond
# S_TABU_NAM display. The table also carries dialog and update queue lengths,
# session counts and database round-trip time, none of which /proc can give.
#
# Requires transaction /SDF/SMON to be scheduled on the target system. Where
# it is not, this returns an empty dict and the tiles stay UNKNOWN -- which
# is correct. "Could not read" is never "healthy".

_SMON_FIELDS = [
    "DATUM", "TIME", "SERVER",
    "IDLE_TOTAL",      # CPU% = 100 - IDLE_TOTAL
    "FREE_MEM_PERC",   # free RAM as % -- EXCLUDES filesystem cache
    "FREE_MEM_MB",
    "FREE_MEM_MB_INC_FS",  # free RAM INCLUDING reclaimable page cache
    "CPU_CONS",        # CPUs consumed -- the true load-average equivalent
    "DIAAVG60",        # dialog queue average over 60s (NOT OS load)
    "DIAQ",            # dialog queue length -- leading indicator
    "UPDQ",            # update queue length
    "USERS",
    "SESSIONS",
    "NETRTT",          # DB round-trip, microseconds
    "AVAILCPUS",
]

# Look back far enough to survive a missed collection cycle without pulling
# a whole day of rows. SMON at a 60s interval gives ~15 rows here.
_SMON_LOOKBACK_MINUTES = 15


def _smon_number(raw):
    """
    RFC_READ_TABLE returns values formatted for the RFC user's locale. This
    system returns "0,09" -- float() on that raises, and stripping the comma
    would give 9.0 instead of 0.09, a hundredfold error that looks entirely
    plausible on a load average. Convert the separator first.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


_ccms_total_cache: dict[str, tuple[float, int | None]] = {}
_CCMS_TOTAL_TTL_S = 3600


def _ccms_total_mb(session: SapSession, system: str) -> int | None:
    """Total RAM in MB from CCMS RZ20, cached per system for an hour."""
    hit = _ccms_total_cache.get(system)
    if hit and time.monotonic() - hit[0] < _CCMS_TOTAL_TTL_S:
        return hit[1]
    total = None
    try:
        total = (_ccms.read_os(session, system) or {}).get("mem_total_mb")
    except Exception:  # noqa: BLE001 -- enrichment only
        total = None
    _ccms_total_cache[system] = (time.monotonic(), total)
    return total


# How far back _smon_uptime walks looking for a restart. SMON retention is
# typically 7-14 days; 30 caps the worst case at ~30 cheap single-day reads
# on the deep clock, and the loop stops early the moment every instance has
# shown a gap. Override with IBO_SMON_UPTIME_DAYS.
_SMON_MAX_DAYS = int(_os.environ.get("IBO_SMON_UPTIME_DAYS", "30") or 30)


def _smon_uptime(session: SapSession, system: str) -> dict:
    """
    Uptime and last downtime per instance, from gaps in /SDF/SMON_HEADER.

    SMON writes one row per instance per interval (60s here). A gap much
    longer than the interval is a downtime; the first row after it is the
    restart. That gives both figures the wall wants -- how long the
    instance has been up, and how long it was last down -- over pure RFC,
    with no kernel start-time FM needed (TH_GET_VIRT_SERVER carries none
    on this release).

    This used to read today + yesterday only. Every system in the fleet had
    been up longer than that, so every card showed the identical floor
    ">= 1d 13h" -- the window, not the uptime. Now it walks back one day at
    a time, most recent first, and stops as soon as a gap is found for
    every instance seen, or after _SMON_MAX_DAYS. A system that restarted
    six days ago now reads "6d 4h"; one that has not restarted within
    SMON's retention reads ">= <retention>", which is the honest answer.

    Honest limits: a gap also appears if the SMON job itself stopped, so
    the note says "no SMON rows", not "instance down".
    """
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()

    def field(r, i):
        try:
            return str(r[i] if r[i] is not None else "").strip()
        except (IndexError, TypeError):
            return ""

    stamps: dict[str, list] = {}
    days_read = 0
    oldest_day = None

    def _all_have_gap() -> bool:
        if not stamps:
            return False
        for ts in stamps.values():
            ts.sort()
            if len(ts) < 2:
                return False
            deltas = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
            interval = deltas[len(deltas) // 2] or 60
            threshold = max(interval * 3, 180)
            if not any((b - a).total_seconds() > threshold for a, b in zip(ts, ts[1:])):
                return False
        return True

    for back in range(_SMON_MAX_DAYS):
        day = (now - _td(days=back)).strftime("%Y%m%d")
        rows = session.read_table("/SDF/SMON_HEADER",
                                  ["DATUM", "TIME", "SERVER"],
                                  f"DATUM = '{day}'", rows=4000)
        if rows is None:
            break                       # table unreadable: stop, keep what we have
        days_read += 1
        if not rows:
            # A whole day with no SMON rows at all. Either retention ends here
            # or SMON was not running; either way older days will not help.
            if back > 0:
                break
            continue
        oldest_day = day
        for r in rows:
            try:
                t = _dt.strptime(field(r, 0) + field(r, 1).rjust(6, "0"),
                                 "%Y%m%d%H%M%S")
            except ValueError:
                continue
            stamps.setdefault(field(r, 2) or "?", []).append(t)
        if _all_have_gap():
            break

    if not stamps:
        return {}

    out: dict[str, dict] = {}
    for server, ts in stamps.items():
        ts.sort()
        if len(ts) < 2:
            continue
        deltas = sorted((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
        interval = deltas[len(deltas) // 2] or 60
        threshold = max(interval * 3, 180)
        last_gap = None
        for a, b in zip(ts, ts[1:]):
            if (b - a).total_seconds() > threshold:
                last_gap = (a, b)
        window_start = ts[0]
        if last_gap:
            restart = last_gap[1]
            downtime_s = int((last_gap[1] - last_gap[0]).total_seconds())
            up_s = int((now - restart).total_seconds())
            out[server] = {
                "uptime_seconds": max(up_s, 0),
                "uptime_text": _fmt_dur(up_s),
                "since": restart.strftime("%Y-%m-%d %H:%M"),
                "last_downtime_seconds": downtime_s,
                "last_downtime_text": _fmt_dur(downtime_s),
                "last_down_from": last_gap[0].strftime("%Y-%m-%d %H:%M"),
                "last_down_to": last_gap[1].strftime("%Y-%m-%d %H:%M"),
                "source": "SMON row gaps",
                "bounded": False,
            }
        else:
            up_s = int((now - window_start).total_seconds())
            out[server] = {
                "uptime_seconds": max(up_s, 0),
                "uptime_text": "\u2265 " + _fmt_dur(up_s),
                "since": None,
                "last_downtime_seconds": None,
                "last_downtime_text": f"none in last {days_read} day(s) of SMON data",
                "source": "SMON row gaps",
                "bounded": True,
            }
    return out


def _fmt_dur(seconds: int) -> str:
    seconds = max(int(seconds), 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _smon(session: SapSession, system: str) -> dict:
    """
    Latest /SDF/SMON_HEADER snapshot, aggregated across ALL application
    servers, or {} if SMON is not scheduled.

    SMON writes one row per instance per cycle. An earlier version took the
    single newest row, which silently reported ONE instance's memory, queues
    and sessions as if they were the whole system -- on a two-instance system
    every figure was understated and the card named only one server.
    """
    now = datetime.now()
    since = now - timedelta(minutes=_SMON_LOOKBACK_MINUTES)

    if since.date() != now.date():
        where = f"DATUM = '{now.strftime('%Y%m%d')}'"
    else:
        where = (f"DATUM = '{now.strftime('%Y%m%d')}' "
                 f"AND TIME >= '{since.strftime('%H%M%S')}'")

    fields = _supported_fields(session, system, "/SDF/SMON_HEADER",
                               ["DATUM", "TIME", "SERVER"],
                               [f for f in _SMON_FIELDS
                                if f not in ("DATUM", "TIME", "SERVER")])
    if not fields:
        return {}
    rows = session.read_table("/SDF/SMON_HEADER", fields, where, rows=500)
    if not rows:
        return {}

    idx = {name: i for i, name in enumerate(fields)}

    def field(row, name):
        i = idx[name]
        return row[i].strip() if i < len(row) else ""

    rows = [r for r in rows if field(r, "TIME")]
    if not rows:
        return {}

    # Newest row PER SERVER. Instances do not always write in the same
    # second, so taking one global maximum would drop the other instance
    # entirely rather than pairing them.
    newest: dict[str, list] = {}
    for r in rows:
        server = field(r, "SERVER") or "?"
        key = (field(r, "DATUM"), field(r, "TIME"))
        if server not in newest or key > (field(newest[server], "DATUM"),
                                          field(newest[server], "TIME")):
            newest[server] = r

    per_server, stamps = [], []
    for server, r in sorted(newest.items()):
        idle = _smon_number(field(r, "IDLE_TOTAL"))
        free_pct = _smon_number(field(r, "FREE_MEM_PERC"))
        free_mb = _smon_number(field(r, "FREE_MEM_MB"))
        free_mb_fs = _smon_number(field(r, "FREE_MEM_MB_INC_FS"))
        # Total RAM is NOT exported by SMON. It was previously derived as
        # free_mb / (free_pct/100) -- but FREE_MEM_PERC is rounded to whole
        # percent, so on a host with 252 MB free of 15,643 MB (1.6%, reported
        # as 1) the derivation returned ~25,200 MB. A 60% error, presented as
        # a precise figure. ST06 is the authority for physical memory; where
        # a number cannot be measured it is not published.
        total_mb = None
        # A measured idle of 100% IS a reading -- an idle QAS box at 0% CPU is
        # real, and the SMON panel shows "CPU IDLE 100%" right next to a tile
        # that said "No data" because this used to discard the derived 0.
        # The "0 is a gap" rule belongs to the Z FM, where 0 means the ABAP
        # side could not read it. Here, the gap case is idle being ABSENT.
        cpu = None if idle is None else round(100.0 - idle, 1)
        per_server.append({
            "server": server,
            "cpu": cpu,
            "cpu_idle": idle,
            "free_mem_mb": free_mb,
            "free_mem_mb_inc_fs": free_mb_fs,
            "total_mem_mb": None if total_mb is None else round(total_mb),
            "memory_excl_cache": None if free_pct is None else round(100.0 - free_pct, 1),
            "load": _smon_number(field(r, "CPU_CONS")),
            "cpus": _smon_number(field(r, "AVAILCPUS")),
            "dialog_queue": _smon_number(field(r, "DIAQ")),
            "update_queue": _smon_number(field(r, "UPDQ")),
            "users": _smon_number(field(r, "USERS")),
            "sessions": _smon_number(field(r, "SESSIONS")),
            "db_rtt_ms": (lambda v: None if v is None else round(v / 1000.0, 1))(
                _smon_number(field(r, "NETRTT"))),
            "dialog_avg_60s": _smon_number(field(r, "DIAAVG60")),
        })
        stamps.append(field(r, "TIME"))

    def total(key):
        vals = [p[key] for p in per_server if p[key] is not None]
        return sum(vals) if vals else None

    def worst(key):
        vals = [p[key] for p in per_server if p[key] is not None]
        return max(vals) if vals else None

    # CPU across instances is weighted by core count -- a 4-core instance at
    # 80% and a 32-core one at 5% is not "42% busy".
    cpu_num = sum((p["cpu"] or 0) * (p["cpus"] or 1)
                  for p in per_server if p["cpu"] is not None)
    cpu_den = sum((p["cpus"] or 1) for p in per_server if p["cpu"] is not None)
    cpu_pct = round(cpu_num / cpu_den, 1) if cpu_den else None

    free_total = total("free_mem_mb")
    free_fs_total = total("free_mem_mb_inc_fs")
    ram_total = total("total_mem_mb")

    # Memory percentage comes straight from FREE_MEM_PERC per instance --
    # the same figure ST06 shows as "Free memory percentage" -- rather than
    # from a derived total. Across instances it is averaged by free MB share
    # only when totals are known; otherwise the worst instance is reported,
    # because a fleet average hides the one that is actually short.
    memory_strict_pct = worst("memory_excl_cache")

    # Two memory figures, and which one the tile shows matters.
    #
    #   memory_excl_cache   100 - FREE_MEM_PERC. Counts reclaimable page cache
    #                       as "used". On Linux/AIX this sits near 100% on a
    #                       perfectly healthy box, because the OS deliberately
    #                       fills free RAM with cache it will hand back on
    #                       demand. Showing this as the memory tile paints a
    #                       fine PRD red -- which is exactly what the wall did.
    #
    #   inc-cache           FREE_MEM_MB_INC_FS adds the reclaimable cache back.
    #                       Where it reports MORE free memory than the strict
    #                       figure, that difference IS reclaimable cache, and
    #                       the cache-inclusive utilisation is the honest
    #                       health signal.
    #
    # So: derive a cache-inclusive percentage per instance where both the
    # inclusive-free and a total are known, take the worst of those, and
    # prefer it. Fall back to the strict figure only when nothing better
    # exists -- and label whichever one is shown so 99% never appears without
    # the reader knowing it is the cache-excluding number.
    def _inc_pct(p):
        free_inc, total = p.get("free_mem_mb_inc_fs"), p.get("total_mem_mb")
        if free_inc is None or not total or total <= 0:
            return None
        return round(100.0 * (1 - free_inc / total), 1)

    inc_candidates = [v for v in (_inc_pct(p) for p in per_server) if v is not None]
    memory_inc_pct = max(inc_candidates) if inc_candidates else None

    if memory_inc_pct is not None and (
            memory_strict_pct is None or memory_inc_pct < memory_strict_pct):
        memory_pct = memory_inc_pct
        memory_basis = "excl. reclaimable cache"
    else:
        memory_pct = memory_strict_pct
        memory_basis = "incl. cache as used" if memory_strict_pct is not None else None

    stamp = max(stamps) if stamps else ""
    servers = [p["server"] for p in per_server]

    return {
        "cpu": cpu_pct,
        "cpu_idle_raw": per_server[0]["cpu_idle"] if len(per_server) == 1 else None,
        "memory": memory_pct,
        "memory_excl_cache": memory_strict_pct,
        "memory_incl_cache": memory_inc_pct,
        "memory_basis": memory_basis,
        "load_1m": total("load"),
        "free_mem_mb": free_total,
        "free_mem_mb_inc_fs": free_fs_total,
        "total_mem_mb": ram_total,
        "dialog_queue": total("dialog_queue"),
        "update_queue": total("update_queue"),
        "dialog_avg_60s": worst("dialog_avg_60s"),
        "users": total("users"),
        "sessions": total("sessions"),
        "db_rtt_ms": worst("db_rtt_ms"),
        "cpus": total("cpus"),
        "instance_count": len(per_server),
        "per_server": per_server,
        "server": ", ".join(servers) if len(servers) <= 2 else f"{len(servers)} instances",
        "sample_time": f"{stamp[0:2]}:{stamp[2:4]}:{stamp[4:6]}" if len(stamp) >= 6 else stamp,
    }


# --------------------------------------------------------------------------
# RZLLITAB -- logon group response time
# --------------------------------------------------------------------------
#
# A previous session spent five rounds hunting function modules (RZL*, TH_*,
# SMLG_*, SAPWL_*) for this. It is a plain table read with RFC_READ_TABLE --
# the same call already used everywhere else here. Check SE16 before SE37.
#
# RESP_TIME is the average dialog response in milliseconds per logon group,
# maintained by the message server. 0 means the group exists but has not been
# measured -- not that the system answered in 0 ms, so it is dropped rather
# than averaged in.

_SMLG_FIELDS = ["CLASSNAME", "APPLSERVER", "GROUPTYPE", "RESP_TIME", "USERS"]


_MSLOAD_RECORD_TYPE = 7353          # VALUE1 of load records, from SAPMSMLG itself
_msload_server: dict[str, str] = {}  # system -> SRVNAME that answered last time


def _smlg_instance_load(session: SapSession, system: str) -> dict:
    """
    Per-instance response exactly as transaction SMLG shows it.

    Traced through SAPMSMLG (include MSMLGF02): the load view is built from
    the message server's shared INTEGER table, read with
    RZL_INTG_READALL_C(SRVNAME = <message server>), keeping records where
    VALUE1 = 7353. Field map, straight from the fill loop in that include:

        NAME   -> instance          VALUE2 -> response ms
        VALUE3 -> dialog steps      VALUE4 -> users
        VALUE5 -> quality           VALUE  -> sample time (HHMMSS)

    Reading the same table SMLG reads makes the wall match SMLG by
    construction. The earlier TH_LOAD_DISTRIBUTION attempt is kept only as
    a trailing fallback: probing showed it does not exist on this kernel
    (FU_NOT_FOUND), but other releases have it.

    If RZL_INTG_READALL_C is not remote-enabled here, every call raises and
    this returns {} -- then the ABAP wrapper in abap/Z_GET_LOGON_LOAD.abap
    (which makes the same call server-side) is the way to get it, and it is
    tried first once installed.
    """
    # Candidate SRVNAMEs: the one that worked last time, then the message
    # server (SERVICES containing 'M'), then every server. The buffer lives
    # with the message server, so the right name is stable per system.
    candidates: list[str] = []
    cached = _msload_server.get(system)
    if cached:
        candidates.append(cached)
    try:
        servers = (session.call("TH_SERVER_LIST") or {}).get("LIST") or []
    except Exception:
        servers = []
    def srv_name(row):
        return str(row.get("NAME", "")).strip()
    msg_first = sorted(servers, key=lambda r: ("M" not in str(r.get("SERVICES", "")).upper()))
    candidates += [srv_name(r) for r in msg_first if srv_name(r)]
    candidates.append("")  # local server -- some kernels resolve this to the MS

    seen: set[str] = set()
    for fm in ("Z_GET_LOGON_LOAD", "RZL_INTG_READALL_C"):
        for srv in candidates:
            if (fm, srv) in seen:
                continue
            seen.add((fm, srv))
            try:
                got = session.call(fm, SRVNAME=srv)
            except Exception:
                got = None
            if not got:
                continue
            rows = got.get("INTG_TBL") or got.get("LOAD_TBL") or []
            out: dict[str, dict] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                if _smon_number(str(r.get("VALUE1", "")).strip()) != _MSLOAD_RECORD_TYPE:
                    continue
                name = str(r.get("NAME", "")).strip()
                if not name:
                    continue
                def n(key):
                    return _smon_number(str(r.get(key, "")).strip())
                out[name] = {
                    "response_ms": round(n("VALUE2")) if n("VALUE2") is not None else None,
                    "dialog_steps": int(n("VALUE3")) if n("VALUE3") is not None else None,
                    "users": int(n("VALUE4")) if n("VALUE4") is not None else None,
                    "quality": round(n("VALUE5")) if n("VALUE5") is not None else None,
                    "sample_time": str(r.get("VALUE", "")).strip() or None,
                }
            if out:
                _msload_server[system] = srv
                summary = ", ".join(
                    f"{k}={v['response_ms']}ms" for k, v in out.items())
                log.info(f"[{system}] SMLG load via {fm}(SRVNAME='{srv}'): {summary}")
                return out

    return _smlg_instance_load_fallback(session, system)


def _smlg_instance_load_fallback(session: SapSession, system: str) -> dict:
    """
    Per-instance response as SMLG's own instance view shows it.

    This is the fix for the dashboard disagreeing with transaction SMLG. The
    old path read RZLLITAB (logon-group CONFIG, whose RESP_TIME is 0 until a
    group carries traffic) and then fell through to raw STAT records, whose
    per-step RESPTI has a long tail -- so an instance SMLG reports at 280 ms
    showed as tens of thousands of ms on the wall.

    TH_LOAD_DISTRIBUTION is the message server's load-balancing table: the
    exact figures SMLG paints (response ms, users, quality, dialog steps per
    application server). Reading it directly makes the two agree by
    construction, because it is the same source SMLG uses.

    Returns {server_name: {response_ms, users, quality, dialog_steps}} or {}
    when the FM is unavailable on this release.
    """
    for fm in ("TH_LOAD_DISTRIBUTION", "TH_GET_LOAD_DISTRIBUTION"):
        try:
            got = session.call(fm)
        except Exception:
            got = None
        if not got:
            continue
        # The table name varies: SERVER_LIST, LIST, LOAD, DISTRIBUTION.
        rows = None
        for key in ("SERVER_LIST", "LIST", "LOAD", "DISTRIBUTION", "SERVERS"):
            if isinstance(got.get(key), list) and got[key]:
                rows = got[key]
                break
        if not rows:
            continue

        out: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("NAME") or row.get("APPLSERVER")
                       or row.get("SERVER") or "").strip()
            if not name:
                continue
            # Field names differ by release; take the first that is present.
            resp = _first_num(row, ("RESPTI", "RESP_TIME", "RESPONSETIME",
                                    "RESPONSE_TIME", "AVG_RESPONSE"))
            users = _first_num(row, ("USERS", "USER", "USERCOUNT", "NUSERS"))
            quality = _first_num(row, ("QUALITY", "QUAL"))
            steps = _first_num(row, ("DIASTEPS", "DIALOG_STEPS", "STEPS",
                                     "DSTEPS"))
            out[name] = {
                "response_ms": round(resp) if resp else None,
                "users": int(users) if users is not None else None,
                "quality": round(quality) if quality is not None else None,
                "dialog_steps": int(steps) if steps is not None else None,
            }
        if out:
            log.info(f"[{system}] SMLG instance load via {fm}: "
                     f"{', '.join(out)}")
            return out
    return {}


def _first_num(row: dict, keys: tuple[str, ...]):
    """First numeric value among candidate column names, or None."""
    for k in keys:
        if k in row:
            v = _smon_number(str(row.get(k, "")).strip())
            if v is not None:
                return v
    return None


def _logon_groups(session: SapSession, system: str) -> dict:
    """Per-group dialog response time from RZLLITAB, or {} if unavailable."""
    fields = _supported_fields(session, system, "RZLLITAB",
                               ["CLASSNAME", "APPLSERVER"],
                               ["GROUPTYPE", "RESP_TIME", "USERS"])
    if not fields:
        return {}
    rows = session.read_table("RZLLITAB", fields, "", rows=100)
    if not rows:
        return {}

    idx = {name: i for i, name in enumerate(fields)}

    def field(row, name):
        i = idx[name]
        return row[i].strip() if i < len(row) else ""

    groups, weighted, users_total = [], [], 0
    for row in rows:
        name = field(row, "CLASSNAME")
        if not name:
            continue
        resp = _smon_number(field(row, "RESP_TIME"))
        users = _smon_number(field(row, "USERS")) or 0
        groups.append({
            "name": name,
            "server": field(row, "APPLSERVER"),
            "response_ms": None if not resp else round(resp),
            "users": round(users),
        })
        # 0 = not measured. Averaging it in would drag a real figure toward
        # zero and report a system as faster than it is.
        if resp:
            weighted.append((resp, max(users, 1)))
            users_total += max(users, 1)

    if not groups:
        # No rows at all: no logon groups are defined in SMLG on this system.
        return {}

    overall = None
    note = None
    if weighted and users_total:
        overall = round(sum(r * u for r, u in weighted) / users_total)
    else:
        # Groups exist but every RESP_TIME is 0. The message server fills that
        # field only once it has measured traffic through the group, so this
        # means "defined but never used", not "answers in 0 ms".
        note = (f"{len(groups)} logon group(s) defined, none with a measured "
                f"response time yet")

    # Per-INSTANCE aggregation. SMLG's own instance view is what a Basis
    # consultant actually reads: one response time per application server,
    # not one per logon group. RZLLITAB carries APPLSERVER alongside
    # RESP_TIME, so the same rows answer both questions.
    #
    # Aggregated by user count, not by simple average: a group with 8 users at
    # 400ms and one with nobody on it at 40ms is not "220ms".
    per_server: dict[str, dict] = {}
    for row in rows:
        server = field(row, "APPLSERVER")
        if not server:
            continue
        resp = _smon_number(field(row, "RESP_TIME"))
        users = _smon_number(field(row, "USERS")) or 0
        entry = per_server.setdefault(server, {"resp_sum": 0.0, "weight": 0,
                                               "idle_sum": 0.0, "idle_count": 0,
                                               "users": 0, "measured": False})
        entry["users"] += int(users)
        if resp:                       # 0 = not measured, not "instant"
            # Weight strictly by user count: a group nobody is logged into
            # contributes no lived experience, and letting it carry weight 1
            # dragged a busy 400ms group down to 360ms in testing. Groups with
            # no users are kept only as a fallback for when NO group has any.
            entry["resp_sum"] += resp * users
            entry["weight"] += users
            entry["idle_sum"] += resp
            entry["idle_count"] += 1
            entry["measured"] = True

    servers = {}
    for server, entry in per_server.items():
        if entry["weight"]:
            response = round(entry["resp_sum"] / entry["weight"])
        elif entry["idle_count"]:
            # Measured, but with nobody logged on anywhere. Report the plain
            # average rather than nothing -- it is a real historic reading,
            # just not one anyone is currently living with.
            response = round(entry["idle_sum"] / entry["idle_count"])
        else:
            response = None
        servers[server] = {
            "response_ms": response,
            "users": entry["users"],
            "measured": entry["measured"],
            "has_users": bool(entry["weight"]),
        }

    return {"groups": groups, "response_ms": overall, "note": note,
            "servers": servers}


# --------------------------------------------------------------------------
# ICM state over RFC
# --------------------------------------------------------------------------
#
# ICM status previously came only from sapcontrol over SSH, so any system
# with has_os_access=false showed ICM as Unknown permanently.
#
# ICM_GET_INFO2 is Remote-Enabled on this release. Its exact interface is not
# assumed: the module is called with no arguments (all parameters optional)
# and the result is searched for the fields below by name. Anything
# unrecognised is reported in `raw_keys` so the mapping can be pinned to the
# real interface rather than guessed -- guessing an SAP interface instead of
# reading it is what cost five rounds on SMLG.

# ICM_GET_INFO2 returns a single INFO_DATA structure of counters and limits
# (max connections, thread pool sizes, queue depth) plus three tables. It has
# NO green/yellow/red status field -- an earlier version read the first
# numeric field it found and rendered "2" as the ICM state.
#
# The state is therefore inferred from reachability, exactly as the
# dispatcher already is below: this module runs inside the ABAP stack and
# queries the ICM directly, so a populated INFO_DATA means the ICM answered.
#
# A FAILED call reports Unknown, never Stopped. It can fail for reasons that
# have nothing to do with the ICM -- missing authorisation, an RFC timeout,
# the module erroring -- and calling that "Stopped" would be a false alarm in
# the opposite direction to the bug this project exists to remove.


def _icm(session: SapSession) -> dict:
    """ICM reachability plus its raw counters, or {} if the call gave nothing."""
    result = session.call("ICM_GET_INFO2")
    if not result:
        return {}

    info = None
    for value in result.values():
        if isinstance(value, dict) and value:
            info = {str(k).upper(): v for k, v in value.items()}
            break

    if info is None:
        info = {str(k).upper(): v for k, v in result.items()
                if not isinstance(v, (list, tuple, dict))}

    if not info:
        return {}

    return {"reachable": True, "counters": info,
            "raw_keys": sorted(info.keys())[:40]}


# --------------------------------------------------------------------------
# Capability cache -- do NOT probe on every poll
# --------------------------------------------------------------------------
#
# WHY THIS EXISTS
# An earlier version of the TBTCO read tried the full field list and, on
# failure, dropped a column and tried again. That looked defensive. What it
# actually did was make failing RFC_READ_TABLE calls on EVERY poll of a
# system that lacked a field -- and the wall polls every 10 seconds. A failed
# table read raises a short dump, so the monitoring became the largest single
# producer of dumps on the system it was watching.
#
# That is the same class of error this project exists to prevent, pointed the
# other way: instead of reporting a healthy-looking lie, it manufactured the
# problem it then reported.
#
# So a table/field combination is probed ONCE per system per process, with a
# tiny read, and the answer is remembered. A system that lacks a field costs
# one failed call in the lifetime of the process, not one every ten seconds.

_capability_lock = threading.Lock()
_capabilities: dict[tuple[str, str], list[str]] = {}


def _supported_fields(session: SapSession, system: str, table: str,
                      required: list[str], optional: list[str]) -> list[str] | None:
    """
    The fields of `table` this system actually has, cached per process.

    Returns None when even the required fields are unavailable -- the caller
    must then skip the read entirely rather than trying a smaller one, since
    a table that rejects its own key fields is not going to answer at all.

    ROWS=1 on the probe: this is asking "does this shape work", not fetching
    data, and a probe that reads 300 rows to answer a schema question is a
    second cost for no extra information.
    """
    key = (system, table)
    with _capability_lock:
        if key in _capabilities:
            cached = _capabilities[key]
            return cached or None

    fields = list(required) + list(optional)
    remaining = list(fields)

    while remaining:
        try:
            session.read_table(table, remaining, "", rows=1)
            with _capability_lock:
                _capabilities[key] = remaining
            dropped = [f for f in fields if f not in remaining]
            if dropped:
                log.info(f"{system}: {table} has no {dropped} on this release "
                         f"-- those columns are omitted from now on.")
            return remaining
        except Exception as exc:
            droppable = next((f for f in optional if f in remaining), None)
            if droppable is None:
                # Required fields missing: remember the failure so the read is
                # never attempted again this process.
                log.warning(f"{system}: {table} is not readable "
                            f"({type(exc).__name__}). Skipping it for this run "
                            f"rather than retrying every poll.")
                with _capability_lock:
                    _capabilities[key] = []
                return None
            remaining.remove(droppable)

    with _capability_lock:
        _capabilities[key] = []
    return None


def reset_capabilities(system: str | None = None) -> None:
    """Forget cached probes -- after a transport, or when a table is added."""
    with _capability_lock:
        if system is None:
            _capabilities.clear()
        else:
            for key in [k for k in _capabilities if k[0] == system]:
                _capabilities.pop(key, None)


# --------------------------------------------------------------------------
# Job detail -- TBTCO
# --------------------------------------------------------------------------
#
# "3 cancelled jobs" is a number, not an incident. What a Basis consultant
# needs before touching anything is: which job, run by whom, started when,
# how long it ran, and whether it ran at all.
#
# THE DISTINCTION THAT MATTERS
# A job that ends at its start time never started -- an authorisation, a
# missing variant, or no free background work process. A job that ran for
# forty minutes and then cancelled failed inside ABAP and has a dump or a job
# log. Those are different faults with different owners, and the cancelled
# count cannot tell them apart. Duration does.

_TBTCO_FIELDS = ["JOBNAME", "JOBCOUNT", "STATUS", "SDLSTRTDT", "SDLSTRTTM",
                 "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME", "AUTHCKNAM",
                 "SDLUNAME"]

# AUTHCKNAM is the step user -- the account the job runs UNDER, very often a
# background service account with no mailbox. SDLUNAME is who SCHEDULED it,
# which is the person who actually wants to know it failed. Both are kept:
# they are frequently different, and notifying the wrong one is worse than
# notifying nobody because it trains the recipient to ignore the next one.

# Dropped first if the full read is rejected. RFC_READ_TABLE fails the whole
# call on one unknown field, so a release without STRTDATE would otherwise
# cost the entire job detail rather than two columns of it.
_TBTCO_OPTIONAL = ["SDLUNAME", "STRTDATE", "STRTTIME", "AUTHCKNAM"]

# A running job past this is worth naming. Below it, "long running" is noise.
LONG_RUNNING_MINUTES = 30


def _stamp(date_text: str, time_text: str) -> datetime | None:
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip() or "000000"
    if not date_text or date_text == "00000000":
        return None
    try:
        return datetime.strptime(f"{date_text}{time_text:0>6}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _jobs(session: SapSession, system: str) -> dict:
    """Cancelled and running jobs for today, with enough detail to act on."""
    today = datetime.now().strftime("%Y%m%d")

    # TWO SHORT READS, NOT ONE LONG OR.
    #
    # RFC_READ_TABLE takes the WHERE as 72-character lines. A combined
    # clause -- "( STATUS = 'A' AND ENDDATE = ... ) OR ( STATUS = 'R' AND
    # ... )" -- is 87 characters, was truncated by SAP, and raised
    # SAPSQL_PARSE_ERROR in SAPLSDTX on every poll. Two clauses of under 40
    # characters each need no splitting at all, so there is nothing for the
    # line handling to get wrong.
    clauses = [f"STATUS = 'A' AND ENDDATE = '{today}'",
               f"STATUS = 'R' AND SDLSTRTDT = '{today}'"]

    required = [f for f in _TBTCO_FIELDS if f not in _TBTCO_OPTIONAL]
    fields = _supported_fields(session, system, "TBTCO", required, _TBTCO_OPTIONAL)
    if not fields:
        return {}

    rows = []
    for clause in clauses:
        try:
            part = session.read_table("TBTCO", fields, clause, rows=300)
            rows.extend(part or [])
        except Exception as exc:
            # The shape was probed and accepted, so a failure here is the
            # query. Reported once rather than retried into a dump.
            log.warning(f"{system}: TBTCO read failed for [{clause}] "
                        f"({type(exc).__name__}: {exc})")
            return {}

    if not rows:
        return {"cancelled": [], "running": [], "long_running": []}

    idx = {name: i for i, name in enumerate(fields)}

    def field(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    now = datetime.now()
    cancelled, running, long_running = [], [], []

    for row in rows:
        name = field(row, "JOBNAME")
        if not name:
            continue

        scheduled = _stamp(field(row, "SDLSTRTDT"), field(row, "SDLSTRTTM"))
        started = _stamp(field(row, "STRTDATE"), field(row, "STRTTIME")) or scheduled
        ended = _stamp(field(row, "ENDDATE"), field(row, "ENDTIME"))

        duration = None
        if started and ended:
            duration = max(0, int((ended - started).total_seconds()))
        elif started and field(row, "STATUS") == "R":
            duration = max(0, int((now - started).total_seconds()))

        entry = {
            "job": name,
            "job_count": field(row, "JOBCOUNT"),
            "user": field(row, "AUTHCKNAM") or None,
            "scheduled_by": field(row, "SDLUNAME") or None,
            "scheduled_at": scheduled.strftime("%H:%M:%S") if scheduled else None,
            "started_at": started.strftime("%H:%M:%S") if started else None,
            "ended_at": ended.strftime("%H:%M:%S") if ended else None,
            "duration_seconds": duration,
            "duration_text": _duration_text(duration),
        }

        if field(row, "STATUS") == "A":
            # Duration 0 with an end time means it ended the second it began.
            entry["never_started"] = duration == 0
            entry["verdict"] = (
                "Ended at its start time -- it never actually ran. Check the "
                "step user's authorisations in SU53, that the variant still "
                "exists, and that a background work process was free."
                if duration == 0 else
                f"Ran for {_duration_text(duration)} then cancelled. The "
                f"failure is inside the job: check ST22 for a dump at "
                f"{entry['ended_at']} and the job log in SM37.")
            # DBA:* is the backup family. A failed backup outranks everything
            # else on this list and should not be read as one of three jobs.
            entry["is_backup"] = name.upper().startswith(("DBA", "SAP_BACKUP"))
            cancelled.append(entry)
        else:
            running.append(entry)
            if duration and duration >= LONG_RUNNING_MINUTES * 60:
                entry["verdict"] = (
                    f"Running {_duration_text(duration)}. Confirm in SM50/SM66 "
                    f"whether it is working or blocked before cancelling it.")
                long_running.append(entry)

    cancelled.sort(key=lambda j: (not j["is_backup"], j["job"]))
    long_running.sort(key=lambda j: -(j["duration_seconds"] or 0))

    # Look up addresses only for jobs that need attention, not every job on
    # the system: two extra table reads per cycle for jobs nobody will be
    # told about is a cost with no return.
    notable = cancelled + long_running
    names = {j.get("scheduled_by") for j in notable} | {j.get("user") for j in notable}
    try:
        emails = _user_emails(session, system, names)
    except Exception:
        emails = {}

    for job in notable:
        scheduler = (job.get("scheduled_by") or "").upper()
        step_user = (job.get("user") or "").upper()
        # Prefer whoever scheduled it: the step user is usually a service
        # account, and a service account's mailbox is nobody's inbox.
        job["owner"] = job.get("scheduled_by") or job.get("user")
        job["owner_email"] = emails.get(scheduler) or emails.get(step_user)

    return {"cancelled": cancelled, "running": running,
            "long_running": long_running,
            "owner_emails_resolved": sum(1 for j in notable if j.get("owner_email")),
            "owners_without_email": sorted(
                {j["owner"] for j in notable
                 if j.get("owner") and not j.get("owner_email")})}


# ---------------------------------------------------------------------------
# Dump detail -- SNAP (who / when / where) plus Z FM rows (program / error)
# ---------------------------------------------------------------------------

def _user_display_names(session: SapSession, system: str, usernames) -> dict:
    """
    SAP username -> "First Last", via USR21 and ADRP.

    Many SAP shops use a numeric personnel ID as the logon name (SU01
    username = employee ID), so a table keyed on username shows "3249"
    instead of a person. USR21 maps that username to a person number;
    ADRP (Business Address Services, person) holds the name against it.

    Unlike _user_emails, this is not gated behind NOTIFY_JOB_OWNERS -- it
    never sends anything, it only labels a name that is already on screen.
    It is still opt-in per call (see read_live's resolve_names) so the
    routine 8s background poll does not pay for two RFC reads that nobody
    is looking at; only an explicit "Analyse live" click asks for it.
    """
    wanted = sorted({str(u).strip().upper() for u in usernames if str(u).strip()})
    if not wanted:
        return {}

    if not _supported_fields(session, system, "USR21", ["BNAME", "PERSNUMBER"], []):
        return {}
    if not _supported_fields(session, system, "ADRP",
                             ["PERSNUMBER", "NAME_FIRST", "NAME_LAST"], []):
        return {}

    def _chunks(names, template, limit=60):
        chunk, length = [], 0
        for name in names:
            term = template.format(name)
            extra = len(term) + (4 if chunk else 0)
            if chunk and length + extra > limit:
                yield chunk
                chunk, length = [term], len(term)
            else:
                chunk.append(term)
                length += extra
        if chunk:
            yield chunk

    persons: dict[str, str] = {}
    for terms in _chunks(wanted, "BNAME = '{}'"):
        where = " OR ".join(terms)
        try:
            rows = session.read_table("USR21", ["BNAME", "PERSNUMBER"], where, rows=200)
        except Exception:
            return {}
        for row in rows or []:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                persons[row[1].strip()] = row[0].strip().upper()

    if not persons:
        return {}

    names: dict[str, str] = {}
    for terms in _chunks(list(persons), "PERSNUMBER = '{}'"):
        where = " OR ".join(terms)
        try:
            rows = session.read_table("ADRP", ["PERSNUMBER", "NAME_FIRST", "NAME_LAST"], where, rows=200)
        except Exception:
            continue
        for row in rows or []:
            if len(row) >= 1 and row[0].strip() in persons:
                first = row[1].strip() if len(row) > 1 else ""
                last = row[2].strip() if len(row) > 2 else ""
                full = " ".join(p for p in (first, last) if p)
                if full:
                    names[persons[row[0].strip()]] = full
    return names


def _dumps(session: SapSession, system: str, zfm_metric=None) -> dict:
    """
    Today's short dumps with the people behind them.

    SNAP is the dump store. Over RFC_READ_TABLE it yields user, time, host,
    client and work process -- enough to say WHO dumped WHEN and WHERE. The
    program and runtime-error name live in FLIST, a long field RFC_READ_TABLE
    cannot return, so those come from Z_GET_OBSERVABILITY_DATA's ET_SHORT_DUMPS
    rows when that FM is installed (the collector already parses them into
    the ST22 metric's detail as "PROGRAM USER, ..."). Without the Z FM the
    program column is honestly absent, not guessed.
    """
    today = datetime.now().strftime("%Y%m%d")
    out = {"count": 0, "by_user": [], "by_host": [], "by_program": [], "recent": [],
           "program_source": None, "note": None}
    # session.read_table() swallows the RFC exception by design (a failed
    # read must not crash the cycle); for this specific table it is worth
    # knowing WHY, because "not readable" covers three different causes an
    # operator would fix differently: no authorization for RFC_READ_TABLE
    # on SNAP (S_TABU_DIS/S_TABU_NAM), the table not existing on this
    # release, or a plain connectivity failure.
    rows, snap_error = None, None
    wanted = ["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "MODNO", "SEQNO"]
    try:
        extra = _snap_extra_fields(session, system)
    except Exception:
        extra = []
    wanted += extra
    if getattr(session, "ok", False) and getattr(session, "conn", None) is not None:
        try:
            res = session.conn.call(
                "RFC_READ_TABLE", QUERY_TABLE="SNAP", DELIMITER="|",
                FIELDS=[{"FIELDNAME": f} for f in wanted],
                OPTIONS=SapSession._split_where(f"DATUM = '{today}'"), ROWCOUNT=2000)
            # BUG FIX. This used to be:
            #     rows = [r["WA"].split("|") for r in res.get("DATA", [])]
            # which produces a list of LISTS, while every reader below calls
            # r.get(FIELDNAME). Lists have no .get, so the first comprehension
            # over `rows` raised AttributeError, the caller's except swallowed
            # it, and dump_detail came back empty on every system -- which is
            # exactly the "no user breakdown" the wall was showing under a
            # perfectly good dump count from the ST22 counter.
            #
            # Take the column order from the echoed FIELDS rather than the
            # request: RFC_READ_TABLE returns them in DDIC order, which is not
            # always the order they were asked for.
            order = [str(f.get("FIELDNAME", "")).strip()
                     for f in res.get("FIELDS", [])] or wanted
            rows = [dict(zip(order, r["WA"].split("|"))) for r in res.get("DATA", [])]
        except Exception as exc:  # noqa: BLE001 -- this is the diagnostic path
            snap_error = f"{type(exc).__name__}: {exc}"
    if rows is None:
        out["note"] = (f"SNAP not readable over RFC -- {snap_error}" if snap_error else
                       "SNAP not readable over RFC (no session)")
        return out

    def f(r, k):
        return str(r.get(k, "") or "").strip()

    # One dump = SEQNO 0 row; older kernels number differently, so fall back
    # to de-duplicating on (time, host, work process).
    heads = [r for r in rows if f(r, "SEQNO").lstrip("0") == ""]
    if not heads:
        seen, heads = set(), []
        for r in rows:
            key = (f(r, "UZEIT"), f(r, "AHOST"), f(r, "MODNO"))
            if key not in seen:
                seen.add(key); heads.append(r)

    from collections import Counter
    by_user = Counter(f(r, "UNAME") or "?" for r in heads)
    by_host = Counter(f(r, "AHOST") or "?" for r in heads)
    out["count"] = len(heads)
    out["by_host"] = [{"host": h, "count": n} for h, n in by_host.most_common(6)]
    heads.sort(key=lambda r: f(r, "UZEIT"), reverse=True)
    out["recent"] = [{"time": f(r, "UZEIT")[:2] + ":" + f(r, "UZEIT")[2:4] + ":" + f(r, "UZEIT")[4:6],
                      "user": f(r, "UNAME"), "host": f(r, "AHOST"), "client": f(r, "MANDT")}
                     for r in heads[:8]]

    # Program / error from the Z FM rows, when present. Detail text is
    # "PROG USER, PROG USER, ..." (see rfc_collector._detail_rows).
    prog = Counter()

    # First choice: a program column read straight off SNAP, when this
    # release has one. Costs nothing extra -- it rides on the read already
    # being made -- and unlike the Z FM path it needs no transport.
    prog_field = next((f for f in _SNAP_PROGRAM_FIELDS if f in wanted), None)
    err_field = next((f for f in _SNAP_ERROR_FIELDS if f in wanted), None)
    if prog_field:
        for r in heads:
            value = f(r, prog_field)
            if value:
                prog[value.upper()] += 1
        if prog:
            out["program_source"] = f"SNAP.{prog_field}"
    if err_field:
        errs = Counter(f(r, err_field).upper() for r in heads if f(r, err_field))
        out["by_error"] = [{"error": e, "count": n} for e, n in errs.most_common(6)]
        for row in out["recent"]:
            row["error"] = None
        for row, r in zip(out["recent"], heads[:8]):
            row["error"] = f(r, err_field) or None
            if prog_field:
                row["program"] = f(r, prog_field) or None

    detail = (getattr(zfm_metric, "detail", "") or "") if zfm_metric is not None else ""
    src = ((getattr(zfm_metric, "extra_data", {}) or {}).get("collector") if zfm_metric is not None else None)
    if not prog and src == "Z_FM" and detail:
        for item in detail.split(","):
            parts = item.strip().split()
            if parts:
                prog[parts[0].upper()] += 1
        out["program_source"] = "Z_GET_OBSERVABILITY_DATA"
    out["by_program"] = [{"program": p, "count": n} for p, n in prog.most_common(6)]
    if not out["by_program"]:
        out["note"] = (
            "no program column on SNAP for this release (probed "
            + ", ".join(_SNAP_PROGRAM_FIELDS) + ") and no Z observability FM "
            "detail; user, time and host below are read directly and are complete")

    try:
        emails = _user_emails(session, system, list(by_user)) if by_user else {}
    except Exception:
        emails = {}
    # Names, not logon IDs. This shop logs on with numeric employee IDs, so
    # "3249 caused 41 dumps" names nobody. The deep read runs once every few
    # minutes now, so the two extra table reads this costs are affordable on
    # the routine path -- they were not when this ran every eight seconds.
    try:
        names = _user_display_names(session, system, list(by_user)) if by_user else {}
    except Exception:
        names = {}

    out["by_user"] = [{"user": u, "name": names.get(u.upper()), "count": n,
                       "share_pct": round(n / max(len(heads), 1) * 100),
                       "email": emails.get(u.upper())}
                      for u, n in by_user.most_common(6)]
    return out


# SNAP field names that carry the failing program on one release or another.
# Probed once per process against the live DDIC rather than assumed: the
# header row's program column is not in the same place on every kernel, and
# guessing produced the "program names not readable over RFC" note on
# systems where a perfectly good column was sitting there unread.
_SNAP_PROGRAM_FIELDS = ("SNAP_PROG", "PROGNAME", "ABAPPROG", "AB_PROGRAM")
_SNAP_ERROR_FIELDS = ("ERRID", "SNAP_ERRID", "AB_ERRORID")


def _snap_extra_fields(session: SapSession, system: str) -> list[str]:
    """
    Which of the optional SNAP columns this system actually has.

    RFC_READ_TABLE fails the whole call if ANY requested field is unknown,
    so the program/error columns cannot simply be added to the main FIELDS
    list -- one missing column on one release would take the user breakdown
    down with it. Probe first, then ask only for what exists.
    """
    # ONE probe, not one per candidate: _capabilities is keyed on
    # (system, table), so a second call for a different field would just get
    # the first call's cached answer back. _supported_fields already drops
    # optional columns one at a time until the read succeeds, which is
    # exactly the narrowing wanted here.
    base = ["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "MODNO", "SEQNO"]
    optional = list(_SNAP_PROGRAM_FIELDS + _SNAP_ERROR_FIELDS)
    got = _supported_fields(session, system, "SNAP", base, optional)
    if not got:
        return []
    return [f for f in got if f in optional]


def _user_emails(session: SapSession, system: str, usernames) -> dict:
    """
    SAP username -> email address, via USR21 and ADR6.

    Two reads because SAP splits it: USR21 maps the user to an address number,
    ADR6 holds the SMTP address against it.

    Returns only what resolves. A username with no mailbox is left out rather
    than guessed at from a naming convention -- inventing user@company.com
    would send a job failure to whoever happens to own that address.
    """
    wanted = sorted({str(u).strip().upper() for u in usernames if str(u).strip()})
    if not wanted:
        return {}

    # Only attempted when owner notification is on. Two extra table reads per
    # cycle to resolve addresses nobody will use is a cost with no return --
    # and on a system where these tables are not readable, it was a dump.
    import os as _os
    if _os.getenv("NOTIFY_JOB_OWNERS", "false").strip().lower() != "true":
        return {}

    if not _supported_fields(session, system, "USR21",
                             ["BNAME", "PERSNUMBER"], ["ADDRNUMBER"]):
        return {}
    if not _supported_fields(session, system, "ADR6",
                             ["PERSNUMBER", "SMTP_ADDR"], []):
        return {}

    # RFC_READ_TABLE's WHERE has a length limit, so this is chunked rather
    # than built as one long OR clause that would be silently truncated.
    # Chunked by CLAUSE LENGTH, not by a fixed count: twenty long usernames
    # produce a 400-character WHERE, and the 72-character line limit is on
    # characters, not on terms.
    def _chunks(names, template, limit=60):
        chunk, length = [], 0
        for name in names:
            term = template.format(name)
            extra = len(term) + (4 if chunk else 0)
            if chunk and length + extra > limit:
                yield chunk
                chunk, length = [term], len(term)
            else:
                chunk.append(term)
                length += extra
        if chunk:
            yield chunk

    persons: dict[str, str] = {}
    for terms in _chunks(wanted, "BNAME = '{}'"):
        where = " OR ".join(terms)
        try:
            rows = session.read_table("USR21", ["BNAME", "PERSNUMBER",
                                               "ADDRNUMBER"], where, rows=200)
        except Exception:
            return {}
        for row in rows or []:
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                persons[row[1].strip()] = row[0].strip().upper()

    if not persons:
        return {}

    emails: dict[str, str] = {}
    numbers = sorted(persons)
    for terms in _chunks(numbers, "PERSNUMBER = '{}'"):
        where = " OR ".join(terms)
        try:
            rows = session.read_table("ADR6", ["PERSNUMBER", "SMTP_ADDR"],
                                      where, rows=200)
        except Exception:
            return {}
        for row in rows or []:
            if len(row) >= 2:
                person, address = row[0].strip(), row[1].strip()
                if person in persons and "@" in address:
                    emails[persons[person]] = address

    return emails


def _duration_text(seconds) -> str | None:
    if seconds is None:
        return None
    if seconds == 0:
        return "0s (never started)"
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


# --------------------------------------------------------------------------
# Locked user detail -- USR02
# --------------------------------------------------------------------------
#
# UFLAG is the whole diagnosis and a bare count hides it. 64 is locked by an
# administrator: housekeeping debt, not an incident. 128 is locked by failed
# logons: either an interface retrying a stale password, or an attack. The
# two look identical in "375 locked users".

_USR02_FIELDS = ["BNAME", "UFLAG", "USTYP", "LOCNT", "TRDAT", "CLASS"]

_USTYP_LABEL = {"A": "dialog", "B": "system", "C": "communication",
                "S": "service", "L": "reference"}


def _locked_users(session: SapSession, system: str) -> dict:
    fields = _supported_fields(session, system, "USR02",
                               ["BNAME", "UFLAG"],
                               ["USTYP", "LOCNT", "TRDAT", "CLASS"])
    if not fields:
        return {}
    try:
        rows = session.read_table("USR02", fields, "UFLAG <> '0'", rows=600)
    except Exception as exc:
        log.warning(f"{system}: USR02 read failed ({type(exc).__name__})")
        return {}
    if not rows:
        return {"admin_locked": 0, "failed_logon_locked": 0, "accounts": []}

    idx = {name: i for i, name in enumerate(fields)}

    def field(row, name):
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    admin, failed, accounts, service_locked = 0, 0, [], []

    for row in rows:
        try:
            flag = int(field(row, "UFLAG") or 0)
        except ValueError:
            continue
        name = field(row, "BNAME")
        kind = _USTYP_LABEL.get(field(row, "USTYP"), field(row, "USTYP") or "?")

        # UFLAG is a bit field: 64 admin lock, 128 failed logons, both possible.
        by_admin = bool(flag & 64)
        by_failures = bool(flag & 128)
        if by_admin:
            admin += 1
        if by_failures:
            failed += 1

        entry = {"user": name, "type": kind, "uflag": flag,
                 "locked_by_admin": by_admin,
                 "locked_by_failed_logons": by_failures,
                 "failed_attempts": field(row, "LOCNT") or "0"}

        if by_failures:
            accounts.append(entry)
        # A locked service or system account breaks an interface silently and
        # is more urgent than a locked dialog user who will simply call.
        if kind in ("system", "service", "communication"):
            service_locked.append(entry)

    accounts.sort(key=lambda a: -int(a["failed_attempts"] or 0))

    return {
        "admin_locked": admin,
        "failed_logon_locked": failed,
        "service_accounts_locked": service_locked[:20],
        "accounts": accounts[:20],
        "verdict": (
            f"{failed} account(s) locked by failed logons -- check SM20 for the "
            f"source terminal or program; a stale password in an interface is "
            f"the usual cause."
            if failed else
            f"All {admin} locks are administrator locks: stale accounts, not a "
            f"security signal."),
    }


def _work_processes(session: SapSession) -> dict:
    """
    Total / in use / free work processes, overall and per type.

    A bare "free" count is not actionable without the denominator: 3 free is
    healthy on a 20-WP system and an emergency on a 4-WP one.
    """
    result = session.call("TH_WPINFO")
    if result is None:
        return {"available": False}

    rows = result.get("WPLIST", []) or []
    if not rows:
        return {"available": False}

    def busy(row) -> bool:
        return not str(row.get("WP_STATUS", "")).strip().lower().startswith("wait")

    by_type: dict[str, dict] = {}
    for row in rows:
        wp_type = str(row.get("WP_TYP", "")).strip().upper() or "OTHER"
        slot = by_type.setdefault(wp_type, {"total": 0, "in_use": 0})
        slot["total"] += 1
        if busy(row):
            slot["in_use"] += 1

    total = len(rows)
    in_use = sum(1 for r in rows if busy(r))

    # The work process executing this very TH_WPINFO call runs SAPLTHFB (the
    # generated program for function group THFB). Reporting it as the longest
    # running process is the monitoring observing itself: it appeared on every
    # poll, always at 0 minutes, always with no user -- which read as a real
    # long-running job to anyone looking at the wall.
    #
    # The other names are the RFC/monitoring plumbing for the same reason.
    _SELF_REPORTS = {"SAPLTHFB", "SAPLTHFC", "SAPLSYST", "SAPMSSY1", "SAPMSSY2"}

    # Below this a "longest running" entry is noise. A process that has been
    # busy for under a minute is doing its job, not stuck, and rounding it to
    # "0m" on the wall implies a problem that is not there.
    _MIN_SECONDS = 60

    longest = None
    for row in rows:
        if not busy(row):
            continue
        report = str(row.get("WP_REPORT", "") or "").strip()
        if report.upper() in _SELF_REPORTS:
            continue
        try:
            seconds = int(str(row.get("WP_ELTIME", "") or 0).strip() or 0)
        except ValueError:
            continue
        if seconds < _MIN_SECONDS:
            continue
        if longest is None or seconds > longest["seconds"]:
            longest = {
                "seconds": seconds,
                "wp": str(row.get("WP_NO", "?")).strip(),
                "user": str(row.get("WP_USER", "") or "").strip(),
                "report": report,
            }

    return {
        "available": True,
        "total": total,
        "in_use": in_use,
        "free": total - in_use,
        "utilization_pct": round(in_use / total * 100) if total else None,
        "by_type": {k: {**v, "free": v["total"] - v["in_use"]}
                    for k, v in sorted(by_type.items())},
        "longest_running": longest,
    }


def _instances(session: SapSession) -> list[dict]:
    """Application servers registered with the message server."""
    result = session.call("TH_SERVER_LIST")
    if result is None:
        return []
    out = []
    for row in result.get("LIST", []) or []:
        out.append({
            "name": str(row.get("NAME", "")).strip(),
            "host": str(row.get("HOST", "")).strip(),
            "services": str(row.get("SERVICES", "")).strip(),
            # TH_SERVER_LIST reports which servers are REGISTERED, not which
            # are responding, so no availability claim is made here.
            "state": "registered",
        })
    return out


def _uptime(session: SapSession, system: str) -> dict:
    """
    ABAP instance uptime, from the kernel start time.

    The START field's name is not stable across kernel releases -- this
    looked for STARTDATE/STARTTIME, found neither on any of the four fleet
    systems, returned {} every time, and the tile fell through to the SMON
    gap floor, which reads ">= 1d 13h" on every card because the lookback
    window is the same for all of them. That is a floor, not an uptime.

    So instead of knowing the field name: scan every scalar the FM returns
    for a YYYYMMDD date and an HHMMSS time that sit next to each other, on
    the FMs that carry instance start information. The first plausible pair
    wins; the source is recorded so the wall can say where it came from.
    Returns {} only when nothing plausible is found, and logs what WAS
    returned once per system so the missing field can be named later.
    """
    import re as _re

    def _pairs(info: dict):
        dates, times = {}, {}
        for k, v in (info or {}).items():
            if isinstance(v, (list, dict)):
                continue
            t = str(v).strip()
            if _re.fullmatch(r"(19|20)\d{6}", t):
                dates[k.upper()] = t
            elif _re.fullmatch(r"\d{6}", t):
                times[k.upper()] = t
        # Prefer a key that says START; a bare date on its own means nothing.
        for dk in sorted(dates, key=lambda k: (0 if "START" in k else 1, k)):
            base = dk.replace("DATE", "").replace("DAT", "").replace("DT", "")
            for tk in sorted(times, key=lambda k: (0 if "START" in k else 1, k)):
                tbase = tk.replace("TIME", "").replace("TIM", "").replace("TM", "")
                if base == tbase or ("START" in dk and "START" in tk):
                    yield dates[dk], times[tk], f"{dk}/{tk}"

    start_dt = None
    source = None
    seen = {}
    for fm in ("TH_GET_VIRT_SERVER", "SAPTUNE_GET_SUMMARY_STATISTIC"):
        try:
            info = session.call(fm) or {}
        except Exception:
            continue
        seen[fm] = sorted(k for k, v in info.items() if not isinstance(v, (list, dict)))
        for date_raw, time_raw, fields in _pairs(info):
            try:
                start_dt = datetime.strptime(date_raw + time_raw, "%Y%m%d%H%M%S")
                source = f"{fm} {fields}"
                break
            except ValueError:
                continue
        if start_dt is not None:
            break

    if start_dt is None:
        # One line per system per process, so the log tells us exactly which
        # fields these kernels DO return instead of silently showing a floor.
        if not getattr(_uptime, "_logged", set()).__contains__(system):
            _uptime._logged = getattr(_uptime, "_logged", set()) | {system}
            log.warning(f"[{system}] kernel start time not found; FM scalar fields were {seen}")
        return {}

    # Measure against the SAP system clock where we have learned it, so the
    # figure is not skewed by the app server's OS timezone (the same offset
    # the lock-age code corrects for).
    with _rp._sap_clock_lock:
        sap_now = _rp._sap_clock.get(system)
    now = sap_now or datetime.now()
    delta = now - start_dt
    total_s = int(delta.total_seconds())
    if total_s < 0:
        # Clock skew larger than the offset correction; don't show a negative.
        return {}

    days, rem = divmod(total_s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {mins}m"
    else:
        text = f"{mins}m"
    return {"uptime_text": text, "uptime_seconds": total_s,
            "started_at": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source}

def read_live(system: str, cfg: dict, use_cache: bool = True, resolve_names: bool = False) -> dict:
    """
    One fast RFC read. Never raises; never writes anything.

    `connected: False` means UNKNOWN. The caller must not render it as
    healthy.

    resolve_names=True additionally resolves numeric SAP usernames (many
    shops log on with an employee ID) to a display name via USR21/ADRP --
    two extra RFC reads, done only on request. The routine background poll
    leaves this off; the on-demand "Analyse live" / AI RCA path turns it on
    because a person is looking at the result right now.
    """
    if use_cache and not resolve_names:
        hit = _cached(system)
        if hit is not None:
            return hit

    payload = {
        "system": system,
        "sid": cfg.get("sap_system_id") or system,
        "client": cfg.get("client", ""),
        "connected": False,
        "error": None,
        "cooldown": 0,
        "read_at": datetime.now().strftime("%H:%M:%S"),
        "cache_age_seconds": 0,
        "cpu": None, "memory": None, "load_1m": None,
        "smon": {},
        "logon_groups": [],
        "job_detail": {},
        "locked_user_detail": {},
        "icm_detail": {},
        "icm_raw_keys": [],
        "dispatcher": {"label": "Unknown", "status": "UNKNOWN"},
        "icm": {"label": "Unknown", "status": "UNKNOWN"},
        "gateway": {"label": "Unknown", "status": "UNKNOWN"},
        "work_processes": {"available": False},
        "instances": [],
        "users": None,
        "response_time": None,
        "checks": [],
        # Complete RFC record: every metric the rfc_collector and rfc_perf
        # collectors produce, with thresholds and extra_data. `checks` is
        # the compact checklist derived from the same objects.
        "rfc_metrics": [],
        "perf": {"metrics": [], "error": None, "age_seconds": None, "read_at": None},
    }

    remaining, cached_error = cooldown_remaining(system)
    if remaining:
        payload["error"] = cached_error
        payload["cooldown"] = remaining
        _store(system, payload)
        return payload

    if not cfg.get("rfc"):
        payload["error"] = "no RFC configured for this system"
        _store(system, payload)
        return payload

    with SapSession(system, cfg) as session:
        if not session.ok:
            payload["error"] = session.error
            # Write the cooldown. Before this, the live refresher read
            # cooldown_remaining() but never set it, so an unreachable host
            # was re-dialled every pass and each attempt blocked on the TCP
            # connect timeout. The refresher waits for all systems, so two
            # dead systems set the cadence for every healthy one.
            park(system, session.error or "")
            remaining, _ = cooldown_remaining(system)
            payload["cooldown"] = remaining
            log.warning(f"[{system}] live read failed, parked for {remaining}s: "
                        f"{session.error}")
            _store(system, payload)
            return payload

        clear_cooldown(system)
        payload["connected"] = True

        fm = session.call("Z_GET_OBSERVABILITY_DATA")
        if fm:
            def number(key):
                try:
                    value = float(str(fm.get(key, "")).strip())
                except (TypeError, ValueError):
                    return None
                # -1 means the ABAP side could not read it. 0 is unavailable
                # for CPU/memory only -- a live host is never at 0% CPU.
                if value < 0:
                    return None
                return value

            # OS tiles are NOT read from the FM. Its EV_CPU_UTIL_PCT /
            # EV_MEM_UTIL_PCT / EV_LOAD_1M exports were SXPG command output
            # (S_LOG_COM, since withdrawn -- abap/SXPG_REMOVAL.md). A system
            # still running the older FM returns numbers for them, and those
            # numbers must be ignored, not displayed. SMON and CCMS below are
            # the only sources for cpu / memory / load_1m.
            payload["users"] = number("EV_ACTIVE_USERS")
            payload["db_type"] = str(fm.get("EV_DB_TYPE", "") or "").strip() or None
            payload["server_time"] = str(fm.get("EV_SYS_TIME", "") or "").strip() or None

            # Dialog response from ST03 workload (SAPWL_ASTAT_DIRECT_READ).
            # -1 is the sentinel for "could not measure" -- either no dialog
            # steps fell in the window, or stat/level is 0 in RZ11. Mapped to
            # None so it cannot render as a 0ms response.
            dialog = number("EV_DIALOG_RESPONSE_MS")
            payload["dialog_response_ms"] = dialog if (
                isinstance(dialog, (int, float)) and dialog >= 0) else None

        # OS metrics from /SDF/SMON_HEADER. Read on the SAME session as the
        # function module above -- a second logon per poll is what filled the
        # audit log and risked account lockout before.
        #
        # These OVERRIDE the function-module values, which are -1 by design.
        # Only overwrite where SMON actually returned a number, so a system
        # without SMON scheduled is not made worse than it already is.
        try:
            smon = _smon(session, system)
        except Exception:
            smon = {}

        # Logon group response time. The docstring above used to claim this
        # was unreachable over RFC; it is a RZLLITAB table read.
        try:
            smlg = _logon_groups(session, system)
        except Exception:
            smlg = {}
        if smlg:
            payload["logon_groups"] = smlg.get("groups", [])
            payload["logon_servers"] = smlg.get("servers", {})
            payload["logon_group_note"] = smlg.get("note")
            if smlg.get("response_ms") is not None:
                # The UI renders response_time as {value, source, age_minutes}
                # -- the shape attach_snapshot_extras uses for carried-forward
                # values. A bare number rendered as "undefined".
                payload["response_time"] = {
                    "value": f"{smlg['response_ms']} ms",
                    "source": "RFC · RZLLITAB",
                    "age_minutes": 0,
                }

        if smon:
            payload["smon"] = smon
            for key in ("cpu", "memory", "load_1m"):
                if smon.get(key) is not None:
                    payload[key] = smon[key]
                    payload["os_source"] = "SMON"
            if smon.get("sessions") is not None:
                payload["sessions"] = smon["sessions"]

            # SMON knows free-including-cache but not total RAM; CCMS knows
            # total RAM. Together they give the honest utilisation
            # (1 - 1870/15987 = 88%) instead of the strict 99% that counts
            # reclaimable cache as used. Total RAM is static, so it is read
            # from CCMS once an hour, not every pass.
            if smon.get("memory_incl_cache") is None and \
                    smon.get("free_mem_mb_inc_fs") is not None:
                total_mb = _ccms_total_mb(session, system)
                if total_mb and 0 <= smon["free_mem_mb_inc_fs"] <= total_mb:
                    pct = round(100.0 * (1 - smon["free_mem_mb_inc_fs"] / total_mb), 1)
                    payload["memory"] = pct
                    smon["memory"] = pct
                    smon["memory_incl_cache"] = pct
                    smon["memory_basis"] = "excl. reclaimable cache"
                    smon["total_mem_mb"] = total_mb
                    smon["total_mem_source"] = "CCMS RZ20"

        # Third source for the OS tiles: RZ20 over the CCMS BAPIs. Needs no
        # Z FM and no SMON schedule -- only saposcol, which runs wherever
        # ST06 works. Only consulted for tiles still empty, and each value
        # it fills is labelled so the wall does not show it as SMON.
        if any(payload.get(k) is None for k in ("cpu", "memory", "load_1m")):
            try:
                ccms = _ccms.read_os(session, system)
            except Exception as exc:  # noqa: BLE001
                ccms = {"error": f"{type(exc).__name__}: {exc}"}
            payload["ccms"] = {k: ccms.get(k) for k in
                               ("per_host", "error", "load_5m", "source",
                                "memory_note", "mem_free_display")}
            for key in ("cpu", "memory", "load_1m"):
                if payload.get(key) is None and ccms.get(key) is not None:
                    payload[key] = ccms[key]
                    payload.setdefault("os_source", "CCMS")
                    payload.setdefault("fallback_sources", {})[key] = {
                        "source": ccms.get("source", "RFC · CCMS RZ20"), "age_minutes": 0}
            if payload.get("load_1m") is None and ccms.get("load_5m") is not None:
                payload["load_5m"] = ccms["load_5m"]
                payload.setdefault("fallback_sources", {})["load_5m"] = {
                    "source": ccms.get("source", "RFC · CCMS RZ20"), "age_minutes": 0}

        # Tell the operator WHY the OS tiles are empty, rather than leaving
        # three blank cards. The usual cause is /SDF/SMON not scheduled.
        if all(payload.get(k) is None for k in ("cpu", "memory", "load_1m")):
            payload.setdefault("os_source", None)
            payload["os_hint"] = (
                "No OS metrics: schedule /SDF/SMON on this system (transaction "
                "/SDF/SMON, 60s interval), or grant the RFC user S_XMI_PROD so "
                "the CCMS RZ20 fallback can read saposcol.")

        # Full T-code counter set, read on the SAME connection. Calling the
        # collector separately would double the number of RFC logons per poll.
        metrics = []
        try:
            metrics = _from_function_module(session) or _from_standard_modules(session)
            payload["checks"] = _checks_from_metrics(metrics)
            payload["check_source"] = "Z_FM" if metrics and metrics[0].extra_data.get(
                "collector") == "Z_FM" else "RFC_TABLE"
        except Exception as exc:
            payload["checks"] = []
            payload["check_error"] = f"{type(exc).__name__}: {exc}"

        # Performance set (SM50 PRIV, SM66 saturation, ST03 response, SM12
        # lock ages, SQLM) on the same connection, on its own refresh clock.
        client = str((cfg.get("rfc") or {}).get("client") or cfg.get("client") or "000")
        perf_metrics, perf_error, perf_age, perf_read_at = _perf_metrics(session, system, client)
        payload["perf"] = {
            "metrics": _serialize_metrics(perf_metrics, perf_read_at, perf_age),
            "error": perf_error,
            "age_seconds": round(perf_age, 1),
            "read_at": perf_read_at,
        }
        # Perf rows join the checklist so the wall and the T-code card show
        # them without a second table; the full record goes in rfc_metrics.
        payload["checks"] = _checks_from_metrics(list(metrics) + list(perf_metrics))
        payload["rfc_metrics"] = (
            _serialize_metrics(metrics, payload["read_at"])
            + payload["perf"]["metrics"]
        )

        # USERS tile: only the Z FM exported EV_ACTIVE_USERS, so systems
        # without it showed "No data" even though the collector had just
        # counted TH_USER_LIST for the AL08 check. Reuse that count. 0 is a
        # real reading here (an idle QAS), not a gap.
        if payload.get("users") is None:
            al08 = next((m for m in metrics if m.name == "sap.al08.user_logons"), None)
            if al08 is not None and al08.value is not None:
                payload["users"] = al08.value
                payload.setdefault("fallback_sources", {})["users"] = {
                    "source": "RFC · TH_USER_LIST", "age_minutes": 0}

        # SERVER TIME / UPTIME: the perf collector learns the SAP clock from
        # STAT record timestamps each poll (it needs it for lock ages).
        # Surface it. Only present when learned -- never the host clock
        # dressed up.
        if not payload.get("server_time"):
            with _rp._sap_clock_lock:
                learned = _rp._sap_clock.get(system)
            if learned is not None:
                payload["server_time"] = learned.strftime("%H:%M:%S")
                payload["server_time_source"] = "RFC · STAT timestamps"

        # Uptime -- the operational figure the tile now shows in place of a
        # bare wall clock. Falls back silently to server_time if the kernel
        # start time is not readable on this release.
        try:
            up = _uptime(session, system)
        except Exception:
            up = {}
        if up:
            payload["uptime_text"] = up["uptime_text"]
            payload["uptime_seconds"] = up["uptime_seconds"]
            payload["uptime_since"] = up["started_at"]
            payload["uptime_source"] = "RFC · " + str(up.get("source") or "kernel start")

        # SMLG instance response, the number transaction SMLG actually shows.
        # Read from the message server's own load table so the dashboard and
        # SMLG agree by construction. This is the top-priority source for the
        # per-instance response below; RZLLITAB and STAT are fallbacks for
        # releases where this FM is unavailable.
        try:
            smlg_load = _smlg_instance_load(session, system)
        except Exception:
            smlg_load = {}
        payload["smlg_load"] = smlg_load

        # Per-instance dialog response from the perf read, for the instance
        # table below. SMLG/RZLLITAB still wins where it exists.
        st03 = next((m for m in perf_metrics if m.name == "sap.st03.dialog_resp_ms"), None)
        payload["perf_response_by_instance"] = dict(
            (st03.extra_data or {}).get("per_instance") or {}) if st03 else {}
        payload["perf_response_window"] = (st03.extra_data or {}).get("window") if st03 else None
        payload["perf_response_low_sample"] = bool((st03.extra_data or {}).get("low_sample")) if st03 else False
        payload["perf_response_steps"] = (st03.extra_data or {}).get("steps") if st03 else None
        payload["perf_read_ok"] = perf_error is None

        payload["work_processes"] = _work_processes(session)

        # SAP-internal memory (ST02) -- the one resource view that does not
        # depend on saposcol, so it still fills on hosts where CPU/host-RAM
        # are legitimately unreadable. Labelled as SAP memory, not memory.
        try:
            from collectors.sap_memory import collect as _sapmem_collect
            sapmem = _sapmem_collect(session, system)
            if sapmem:
                metrics = list(metrics) + list(sapmem)
                # LAST RESORT for the memory tile.
                #
                # Host RAM has three sources above (Z FM, SMON, RZ20) and a
                # fourth in attach_snapshot_extras (the last SSH sweep). Where
                # a system has none of them -- no Z FM, no SMON schedule, no
                # memory total published by saposcol, no OS access -- the tile
                # read "No data" forever, which is honest but tells an
                # operator nothing about a box that might be swapping.
                #
                # ST02 extended memory is NOT host RAM and must never be
                # relabelled as such. It is SAP's own allocated memory, it
                # needs neither saposcol nor OS access, and running out of it
                # is a real and common production incident. So it fills the
                # tile only when everything else is empty, and it is tagged
                # with its own source so the UI shows "SAP memory (ST02)"
                # rather than passing it off as the machine's.
                if payload.get("memory") is None:
                    em = next((m for m in sapmem
                               if m.name == "sap.st02.extended_memory_pct"), None)
                    if em is not None and em.value is not None:
                        payload["memory"] = em.value
                        payload["memory_is_sap_internal"] = True
                        payload.setdefault("fallback_sources", {})["memory"] = {
                            "source": "RFC · ST02 extended memory (SAP, not host RAM)",
                            "age_minutes": 0,
                        }
        except Exception as exc:  # noqa: BLE001 -- optional enrichment only
            log.info(f"[{system}] SAP memory collector skipped: "
                     f"{type(exc).__name__}: {exc}")
        # Detail behind the counters. A count is not an incident: the job
        # name, its user, its duration and whether it ran at all are what
        # someone acts on.
        #
        # DEEP READ -- about fifteen RFC round trips (SNAP scan, TBTCO job
        # detail, USR02 locked users, and the USR21/ADRP name lookups behind
        # them). None of it moves meaningfully inside a minute, so it runs on
        # _DEEP_TTL_SECONDS rather than every pass. Between deep passes the
        # last result is carried forward and labelled with its age -- stale
        # but honest, which is the same contract save_snapshot() uses for
        # GUI evidence.
        deep = _deep_cached(system)
        if deep is None:
            deep = {}
            try:
                deep["job_detail"] = _jobs(session, system)
            except Exception:
                deep["job_detail"] = {}
            try:
                deep["locked_user_detail"] = _locked_users(session, system)
            except Exception:
                deep["locked_user_detail"] = {}
            try:
                st22 = next((m for m in metrics if m.name == "sap.st22.dumps"), None)
                deep["dump_detail"] = _dumps(session, system, st22)
            except Exception as exc:
                deep["dump_detail"] = {"count": None,
                                       "note": f"{type(exc).__name__}: {exc}"}
            try:
                deep["smon_uptime"] = _smon_uptime(session, system) if payload.get("smon") else {}
            except Exception:
                deep["smon_uptime"] = {}
            _deep_store(system, deep)
            deep = dict(deep, _age_seconds=0.0)

        payload["job_detail"] = deep.get("job_detail") or {}
        payload["locked_user_detail"] = deep.get("locked_user_detail") or {}
        payload["dump_detail"] = deep.get("dump_detail") or {}
        payload["deep_age_seconds"] = deep.get("_age_seconds", 0.0)

        # Uptime / last downtime from SMON gaps, when the kernel start time
        # was not readable. Worst instance (shortest uptime) headlines.
        up_by_server = deep.get("smon_uptime") or {}
        if up_by_server and not payload.get("uptime_text"):
            worst_up = min(up_by_server.values(), key=lambda u: u["uptime_seconds"])
            payload["uptime_text"] = worst_up["uptime_text"]
            payload["uptime_seconds"] = worst_up["uptime_seconds"]
            payload["uptime_since"] = worst_up.get("since")
            payload["uptime_source"] = "RFC · SMON row gaps"
            payload["last_downtime_text"] = worst_up.get("last_downtime_text")
            payload["last_down_from"] = worst_up.get("last_down_from")
            payload["last_down_to"] = worst_up.get("last_down_to")
            payload["uptime_bounded"] = worst_up.get("bounded", False)
        payload["uptime_by_instance"] = up_by_server

        # The live ST22 counter is cheap and read every pass. If it has moved
        # since the last deep pass, the breakdown below it is out of date and
        # the UI must be able to say so rather than pair a fresh count with a
        # stale set of names.
        st22_live = next((m for m in metrics if m.name == "sap.st22.dumps"), None)
        if st22_live is not None and st22_live.value is not None:
            detail_count = (payload["dump_detail"] or {}).get("count")
            payload["dump_detail"] = dict(payload["dump_detail"] or {})
            payload["dump_detail"]["live_count"] = st22_live.value
            if detail_count is not None and detail_count != st22_live.value:
                payload["dump_detail"]["breakdown_stale"] = True

        if resolve_names:
            try:
                wanted = {u.get("user") for u in (payload["dump_detail"] or {}).get("by_user", [])}
                lk = next((m for m in perf_metrics if m.name == "sap.sm12.locks_per_user_max"), None)
                if lk is not None:
                    wanted |= {u[0] if isinstance(u, (list, tuple)) else u.get("user")
                              for u in (lk.extra_data or {}).get("top_users", [])}
                payload["user_names"] = _user_display_names(session, system, wanted) if wanted else {}
            except Exception:
                payload["user_names"] = {}

        payload["instances"] = _instances(session)

        # Fold per-instance figures onto the instance rows. Services from
        # TH_SERVER_LIST was empty on every system seen so far.
        #
        # THREE SOURCES, AND THEY MEASURE DIFFERENT THINGS:
        #
        #   RZLLITAB USERS      users who connected VIA A LOGON GROUP. Reads 0
        #                       on a system where people connect straight to
        #                       the instance -- which is not "no users".
        #   SMON USERS          users on the instance, however they connected.
        #                       This is the number SMLG's instance view shows.
        #   RZLLITAB RESP_TIME  response per logon group. SMLG's instance view
        #                       gets its figure from the message server, which
        #                       is NOT this table -- hence a group reading 0
        #                       while SMLG shows 3ms for the same instance.
        #
        # So users come from SMON, and response falls back through the sources
        # that actually carry it, labelled with which one answered. A number
        # whose origin is unstated cannot be checked.
        by_group = payload.get("logon_servers") or {}
        by_smon = {p.get("server"): p for p in (smon.get("per_server") or [])} \
            if isinstance(smon, dict) else {}

        for inst in payload["instances"]:
            name = inst.get("name")
            group_stats = by_group.get(name) or {}
            smon_stats = by_smon.get(name) or {}

            users = smon_stats.get("users")
            if users is None:
                users = group_stats.get("users")
            inst["users"] = int(users) if isinstance(users, (int, float)) else None
            inst["sessions"] = smon_stats.get("sessions")

            response = None
            source = None
            statistic = None
            smon_idle = False   # SMON explicitly reported no dialog in 60s

            # 1. SMLG load table -- the exact figure transaction SMLG shows.
            #    This is the authoritative source and matches the GUI by
            #    construction, so it is tried first.
            smlg_stats = (payload.get("smlg_load") or {}).get(name) or {}
            if smlg_stats.get("response_ms") is not None:
                response = smlg_stats["response_ms"]
                source = "SMLG"
                statistic = "average"
                if smlg_stats.get("users") is not None and inst.get("users") is None:
                    inst["users"] = smlg_stats["users"]
                if smlg_stats.get("dialog_steps") is not None:
                    inst["dialog_steps"] = smlg_stats["dialog_steps"]
                if smlg_stats.get("quality") is not None:
                    inst["quality"] = smlg_stats["quality"]

            # 1b. SMON's per-instance 60-second dialog average -- live, per
            #     server, pure RFC (/SDF/SMON_HEADER). The closest thing to
            #     SMLG's column without ABAP: SMLG is a message-server
            #     rolling average, this is a 60s window. A value of 0 means
            #     no dialog steps in the window, i.e. idle -- not 0 ms.
            if response is None:
                perf_resp = (payload.get("perf_response_by_instance") or {}).get(name)
                if isinstance(perf_resp, (int, float)):
                    response = perf_resp
                    window = payload.get("perf_response_window") or "STAT"
                    source = f"STAT · {window}"
                    statistic = "median"
                    # This branch used to check ONLY low-sample, never
                    # aggregate -- so a whole-day ST03N median (plenty of
                    # steps, just old ones) sailed through unflagged and
                    # painted red exactly like a live 5-minute reading.
                    # An aggregate window is not comparable to SMLG's
                    # live figure regardless of step count.
                    steps = payload.get("perf_response_steps")
                    aggregate = "aggregate" in (window or "")
                    if payload.get("perf_response_low_sample") or aggregate:
                        inst["response_low_confidence"] = True
                        inst["response_confidence_note"] = (
                            "daily ST03N aggregate — no dialog steps in the "
                            "live window" if aggregate else
                            f"only {steps} dialog step(s) in the window — "
                            f"near idle, not comparable to SMLG")

            # 2. RZLLITAB logon-group response (config table).
            if response is None:
                response = group_stats.get("response_ms")
                if response is not None:
                    source = "SMLG logon group"
                    statistic = "average"

            if response is None:
                # 3. Dialog response from ST03 workload, via the function
                #    module. Returns -1 until stat/level is enabled in RZ11.
                fm_response = payload.get("dialog_response_ms")
                if isinstance(fm_response, (int, float)) and fm_response >= 0:
                    response = fm_response
                    source = "ST03 workload"
                    statistic = "average"

            # Idle (STAT read fine, just zero steps this window) is now
            # handled by the idle branch below, not by falling back to a
            # whole-day aggregate. An instance with no traffic in the last
            # 5 minutes is idle -- that IS the answer, not a gap to paper
            # over with yesterday's median. The aggregate fallback now
            # only fires when the live STAT read itself failed.
            if response is None and not smon_idle and not payload.get("perf_read_ok"):
                # 4. Last resort: the perf collector's STAT read (or its ST03N
                #    daily aggregate). Per-step RESPTI has a long tail, so this
                #    is a median and clearly labelled as such -- it is why the
                #    wall previously disagreed with SMLG, and it now only shows
                #    when nothing better exists.
                perf_resp = (payload.get("perf_response_by_instance") or {}).get(name)
                if isinstance(perf_resp, (int, float)):
                    response = perf_resp
                    window = payload.get("perf_response_window") or "STAT"
                    source = f"STAT · {window}"
                    statistic = "median"
                    # SMLG's live per-instance response is not exposed over
                    # standard RFC on this kernel (probed: every load FM is
                    # FU_NOT_FOUND; RZLLITAB is config-only). This STAT figure
                    # is the best RFC can do, but on an idle system it is
                    # either a median over a handful of steps or a whole-day
                    # aggregate -- neither is comparable to SMLG's 156 ms and
                    # neither deserves a red headline. Flag it so the wall
                    # shows it muted with the reason, not as a verdict.
                    steps = payload.get("perf_response_steps")
                    aggregate = "aggregate" in (window or "")
                    if payload.get("perf_response_low_sample") or aggregate:
                        inst["response_low_confidence"] = True
                        inst["response_confidence_note"] = (
                            "daily ST03N aggregate -- no dialog steps in the "
                            "live window" if aggregate else
                            f"only {steps} dialog step(s) in the window -- "
                            f"near idle, not comparable to SMLG")

            inst["response_ms"] = response
            inst["response_source"] = source
            inst["response_measured"] = response is not None
            inst["response_statistic"] = statistic
            # STAT was readable and simply held no dialog steps: that is
            # "idle", which is a different statement from "not measured".
            #
            # Idle is reported as a NULL reading plus a note, never as 0 ms.
            # Zero in a response-time field reads as the best possible result,
            # so an instance nobody used would have outranked every instance
            # actually serving users. The wall already renders exactly this
            # shape -- see the response_note branch in wall.html, which was
            # unreachable for as long as this wrote a number.
            if response is None and payload.get("perf_read_ok"):
                inst["response_ms"] = None
                inst["response_source"] = "idle"
                inst["response_statistic"] = None
                inst["response_measured"] = True
                inst["response_note"] = (
                    "STAT read succeeded but this instance had no dialog steps "
                    "in the window -- idle, not unmeasured.")

        # AL08 USER SESSIONS: TH_USER_LIST only lists users on the instance
        # the RFC connection happened to land on, and on these systems it
        # answers empty -- so the wall said "0 count" beside an instance
        # table showing 152 users. The per-instance figures (SMON, then
        # SMLG) are system-wide and match transaction SMLG by construction,
        # so when they add up to more than the AL08 read, they win.
        _inst_users = [i.get("users") for i in payload.get("instances") or []
                       if isinstance(i.get("users"), (int, float))]
        if _inst_users:
            _al08_total = int(sum(_inst_users))
            for _row in payload.get("checks") or []:
                if _row.get("metric") != "sap.al08.user_logons":
                    continue
                try:
                    _al08_read = int(str(_row.get("value", "0")).split()[0])
                except (ValueError, IndexError):
                    _al08_read = 0
                if _al08_total > _al08_read:
                    _warn, _crit = 150, 250
                    _row["value"] = f"{_al08_total} count"
                    _row["status"] = ("CRITICAL" if _al08_total >= _crit
                                      else "WARNING" if _al08_total >= _warn
                                      else "NORMAL")
                    _row["detail"] = (f"sum of {len(_inst_users)} instance(s); "
                                      f"TH_USER_LIST read {_al08_read}")
            if (payload.get("users") or 0) < _al08_total:
                payload["users"] = _al08_total
                payload.setdefault("fallback_sources", {})["users"] = {
                    "source": "RFC \u00b7 per-instance (SMON/SMLG)", "age_minutes": 0}

        # Dispatcher / ICM / gateway state. TH_WPINFO answering at all proves
        # the dispatcher is serving requests -- we are talking to it.
        if payload["work_processes"].get("available"):
            payload["dispatcher"] = {"label": "Running", "status": "NORMAL"}
        for key, count_key in (("icm", "EV_ICM_STATE"), ("gateway", "EV_GW_STATE")):
            if fm and count_key in fm:
                label, status = _process_state(fm.get(count_key))
                payload[key] = {"label": label, "status": status}

        # ICM over RFC. Only overrides when the call actually returned a
        # state -- a system with no ICM_GET_INFO2 keeps whatever the sweep
        # or the function module supplied.
        try:
            icm = _icm(session)
        except Exception:
            icm = {}
        if icm.get("reachable"):
            payload["icm"] = {"label": "Running", "status": "NORMAL",
                              "source": "RFC · ICM_GET_INFO2"}
            payload["icm_detail"] = icm.get("counters") or {}
            payload["icm_raw_keys"] = icm.get("raw_keys") or []

    _store(system, payload)
    return payload


def attach_snapshot_extras(payload: dict, snapshot: dict | None) -> dict:
    """
    Fills tiles the live RFC read could not supply, from the last sweep.

    Every value carries `source` and `age_minutes` so the UI can label it.
    Mixing a two-hour-old GUI reading into a live tile without saying so is
    how a dashboard starts lying quietly.

    Why this is needed per tile:
      * memory  -- the function module exports EV_MEM_UTIL_PCT only if the
                   corrected Z FM is installed; the SSH collector always has
                   it where OS access exists.
      * ICM / dispatcher -- there is no RFC call for these. sapcontrol
                   GetProcessList is the authoritative source and only the
                   SSH collector runs it.
      * response time -- falls back to the last sweep when RZLLITAB is empty
                   on this release; the SAP GUI collector captures it.
    """
    if not snapshot:
        return payload

    metrics = {m.get("name"): m for m in snapshot.get("metrics", []) or []}
    stamp = snapshot.get("cycle_timestamp") or snapshot.get("generated_at")

    age_minutes = None
    if stamp:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                age_minutes = max(0, round(
                    (datetime.now() - datetime.strptime(str(stamp), fmt)).total_seconds() / 60
                ))
                break
            except ValueError:
                continue

    def numeric_fallback(field: str, metric_name: str):
        """Fills a numeric tile only when the live read produced nothing."""
        if payload.get(field) is not None:
            return
        hit = metrics.get(metric_name)
        if not hit:
            return
        try:
            payload[field] = float(str(hit.get("display_value", "")).rstrip("%").split()[0])
        except (ValueError, IndexError):
            return
        # Record where it came from so the tile is not shown as RFC-live.
        payload.setdefault("fallback_sources", {})[field] = {
            "source": hit.get("source") or "last sweep",
            "age_minutes": age_minutes,
        }

    numeric_fallback("cpu", "cpu")
    numeric_fallback("memory", "memory")
    numeric_fallback("load_1m", "load_1m")

    def process_state(field: str, metric_name: str):
        """Dispatcher / ICM / gateway, from sapcontrol via the SSH collector."""
        if payload.get(field, {}).get("status") not in (None, "UNKNOWN"):
            return
        hit = metrics.get(metric_name)
        if not hit:
            return
        colour = str(hit.get("display_value", "")).strip().upper()
        label = {"GREEN": "Running", "YELLOW": "Starting",
                 "RED": "Stopped"}.get(colour, colour.title() or "Unknown")
        payload[field] = {
            "label": label,
            "status": (hit.get("status") or "UNKNOWN"),
            "source": "sapcontrol",
            "age_minutes": age_minutes,
        }

    process_state("dispatcher", "sap_process_disp+work")
    process_state("icm", "sap_process_icman")
    process_state("gateway", "sap_process_gwrd")

    def carry(target, metric_name, label):
        hit = metrics.get(metric_name)
        if not hit:
            return
        payload[target] = {
            "value": hit.get("display_value"),
            "status": hit.get("status", "UNKNOWN"),
            "source": label,
            "age_minutes": age_minutes,
            # Finer grade than Status can express, set by the SMLG analyzer.
            # Without it the wall can show a 2600 ms response as an ordinary
            # red chip, indistinguishable from a 2100 ms one.
            "alert_level": (hit.get("extra_data") or {}).get("alert_level"),
        }

    carry("response_time", "sap.smlg.response_time", "SAP GUI · SMLG")
    carry("dialog_response", "sap.st03n.dialog_response_time", "SAP GUI · ST03N")

    payload["snapshot_age_minutes"] = age_minutes
    payload["snapshot_status"] = snapshot.get("overall_status")
    payload["open_incidents"] = len(snapshot.get("incidents") or [])
    return payload

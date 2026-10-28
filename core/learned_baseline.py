"""
Learned normal values: "unusual for this system at this time of day".

Fixed thresholds treat Monday 09:00 (everyone logging on, 600 locks, dialog
queue of 4) the same as Sunday 03:00. This learns, per system and metric,
what is normal for each hour of the week from the history already stored
(core/metric_history.py: every live read and every sweep), and compares a
new reading with that.

THE BAND (plain language)
For the same weekday and hour, take the middle value of past readings
(the median) and how far readings usually stray from it (the median
absolute deviation). "Normal" is the middle value give or take three of
those typical strays. Using the middle value and typical stray -- not the
average -- means one bad Monday does not stretch what counts as normal.

Not enough history at that weekday-hour (fewer than 3 different weeks)?
Use the same hour on all weekdays (or all weekend days) instead, if that
has at least 8 readings from 5 different days. Less than that: "learning",
and nothing changes.

WHAT IT DOES (IBO_LEARNED_MODE)
  smart (default)
      * a WARNING from a fixed threshold that is normal for this hour goes
        back to NORMAL, with the reason in the detail ("normal for Mon
        09:00: 420-760, 6 weeks of history");
      * a NORMAL reading far ABOVE what is normal for this hour becomes a
        WARNING ("unusual for Sun 03:00: normally 0-2");
      * CRITICAL is never changed. Metrics in never_downgrade (failed
        updates, IDocs, backups) are never turned down.
  annotate   only adds the note; statuses unchanged
  off        nothing

The metrics it applies to and the knobs are in config/learned_baselines.yaml
(optional; defaults below).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_INCLUDE = [
    "cpu", "memory", "load_1m", "dialog_queue", "update_queue", "db_rtt_ms", "sessions",
    "sap.al08.user_logons", "sap.sm12.lock_count", "sap.sm50.wp_in_use", "sap.sm66.wp_saturation_pct",
    "sap.sm50.wp_utilization", "sap.st03.dialog_resp_ms", "sap.st03.max_instance_resp_ms",
    "sap.smlg.response_time", "sap.st22.dumps", "sap.sm37.cancelled_jobs", "sap.sm58.stuck_entries",
    "sap.smq1.entries", "sap.smq2.entries", "sap.su01.locked_users",
    "synthetic.logon_ms", "synthetic.total_ms",
]
DEFAULT_NEVER_DOWNGRADE = ["sap.sm13.failed_updates", "sap.we02.failed_idocs", "sap.db12.last_backup",
                           "rfc.connection"]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_cache_lock = threading.Lock()
_series_cache: dict[str, tuple[float, dict[str, list[tuple[datetime, float]]]]] = {}
# 56 days of samples barely move in an hour; reading them all again for every
# hourly sweep took minutes on CEAT S4P (28.10.2026) and the sweep watchdog
# killed the run as hung. IBO_LEARNED_CACHE_MIN / IBO_LEARNED_LOAD_BUDGET_S.
_CACHE_SECONDS = max(60, int(float(os.getenv("IBO_LEARNED_CACHE_MIN", "60") or 60) * 60))
_LOAD_BUDGET_S = float(os.getenv("IBO_LEARNED_LOAD_BUDGET_S", "60") or 60)


def mode() -> str:
    m = (os.getenv("IBO_LEARNED_MODE") or settings().get("mode") or "smart").strip().lower()
    return m if m in ("smart", "annotate", "off") else "smart"


_settings_cache: dict = {"stamp": None, "data": {}}


def settings() -> dict:
    try:
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "learned_baselines.yaml")
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
        if _settings_cache["stamp"] != stamp:
            import yaml
            with open(path, "r", encoding="utf-8") as fh:
                _settings_cache.update(stamp=stamp, data=yaml.safe_load(fh) or {})
        return _settings_cache["data"] if isinstance(_settings_cache["data"], dict) else {}
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/learned_baselines.yaml unreadable: {exc}")
        return {}


def _num(key: str, default: float) -> float:
    try:
        return float(os.getenv(f"IBO_LEARNED_{key.upper()}") or settings().get(key) or default)
    except (TypeError, ValueError):
        return default


def included(metric: str) -> bool:
    s = settings()
    inc = s.get("include") or DEFAULT_INCLUDE
    exc = s.get("exclude") or []
    return metric in inc and metric not in exc


def never_downgrade(metric: str) -> bool:
    return metric in (settings().get("never_downgrade") or DEFAULT_NEVER_DOWNGRADE)


# ------------------------------------------------------------------ history

def _load_system(system: str, days: int = 56) -> dict[str, list[tuple[datetime, float]]]:
    """All numeric samples for one system, grouped by metric. One pass, cached 10 min."""
    with _cache_lock:
        hit = _series_cache.get(system)
        if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
            return hit[1]
    from core.metric_history import HISTORY_DIR, _safe
    from core.atomic_json import read_lines
    cutoff = datetime.now() - timedelta(days=days)
    months, cursor = set(), cutoff.replace(day=1)
    while cursor <= datetime.now():
        months.add(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    started = time.monotonic()
    rows = 0
    stopped = False
    try:
        from core import heartbeat as _hb
    except Exception:  # noqa: BLE001
        _hb = None
    # Newest month first: if the time budget runs out, the recent weeks count.
    for month in sorted(months, reverse=True):
        if stopped:
            break
        path = os.path.join(HISTORY_DIR, _safe(system), f"{month}.jsonl")
        try:
            for line in read_lines(path):
                rows += 1
                if rows % 5000 == 0:
                    if _hb is not None:
                        try:
                            _hb.beat(f"{system}:learned-baseline")   # a long read is not a hang
                        except Exception:  # noqa: BLE001
                            pass
                    if time.monotonic() - started > _LOAD_BUDGET_S:
                        log.warning(f"{system}: learned baseline read stopped after {_LOAD_BUDGET_S:.0f}s "
                                    f"({rows} samples, from {month}); using what was read.")
                        stopped = True
                        break
                try:
                    row = json.loads(line)
                    v = row.get("v")
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    t = datetime.fromisoformat(row["t"])
                except (ValueError, KeyError, TypeError):
                    continue
                if t >= cutoff:
                    out[row.get("m", "")].append((t, float(v)))
        except Exception:
            continue
    data = dict(out)
    with _cache_lock:
        _series_cache[system] = (time.monotonic(), data)
    return data


def forget(system: str | None = None) -> None:
    with _cache_lock:
        if system is None:
            _series_cache.clear()
        else:
            _series_cache.pop(system, None)


# ---------------------------------------------------------------- the band

def _band(values: list[float], k: float, floor: float) -> tuple[float, float, float]:
    med = median(values)
    mad = median([abs(v - med) for v in values])
    spread = max(1.4826 * mad, floor, abs(med) * 0.05)
    return med, med - k * spread, med + k * spread


def assess(system: str, metric: str, value: float, when: datetime | None = None,
           series: list[tuple[datetime, float]] | None = None) -> dict:
    """{state: normal|high|low|learning, median, low, high, samples, basis}."""
    when = when or datetime.now()
    if series is None:
        series = _load_system(system).get(metric, [])
    # Exclude the reading being judged (and anything after it).
    series = [(t, v) for t, v in series if t < when - timedelta(minutes=2)]
    k = _num("k", 3.0)
    floor = _num("floor", 1.0) if not metric.startswith(("cpu", "memory")) else 2.0
    hour, wd = when.hour, when.weekday()

    # Business calendar (core/business_calendar.py): month-end, year-end,
    # holidays and campaign days are compared with days of their own kind,
    # and never train the band for ordinary days.
    from core.business_calendar import day_class
    cls = day_class(when)
    if cls in ("special", "holiday"):
        own = [(t, v) for t, v in series if day_class(t) == cls and t.hour == hour]
        own_days = {t.date() for t, _ in own}
        if len(own_days) >= 3:
            med, lo, hi = _band([v for _, v in own], k, floor)
            lo = max(lo, 0.0) if min(v for _, v in own) >= 0 else lo
            state = "high" if value > hi else "low" if value < lo else "normal"
            label = "month-end / special days" if cls == "special" else "holidays"
            return {"state": state, "median": round(med, 2), "low": round(lo, 2), "high": round(hi, 2),
                    "samples": len(own), "basis": f"{label} {hour:02d}:00, {len(own_days)} such days of history"}
        # Not enough days of this kind yet: judge against ordinary days, with
        # a band half as wide again, and say so.
        k *= 1.5
    series = [(t, v) for t, v in series if day_class(t) not in ("special", "holiday")]
    weekend_like = cls in ("weekend", "holiday")

    same_slot = [(t, v) for t, v in series if t.weekday() == wd and t.hour == hour]
    weeks = {t.isocalendar()[:2] for t, _ in same_slot}
    min_weeks = int(_num("min_weeks", 3))
    if len(weeks) >= min_weeks and len(same_slot) >= min_weeks and cls != "holiday":
        values = [v for _, v in same_slot]
        basis = f"{DAYS[wd]} {hour:02d}:00, {len(weeks)} weeks of history"
    else:
        same_hour = [(t, v) for t, v in series if (day_class(t) == "weekend") == weekend_like and t.hour == hour]
        days = {t.date() for t, _ in same_hour}
        if len(same_hour) >= 8 and len(days) >= 5:
            values = [v for _, v in same_hour]
            basis = f"{'weekends' if weekend_like else 'weekdays'} {hour:02d}:00, {len(days)} days of history"
        else:
            have = len({t.date() for t, _ in series})
            return {"state": "learning", "samples": len(series), "basis":
                    f"learning: {have} day(s) of history so far; needs readings at "
                    f"{DAYS[wd]} {hour:02d}:00 from {min_weeks} different weeks (or 5 days at this hour)"}
    if cls in ("special", "holiday"):
        basis += f" (today is a {'holiday' if cls == 'holiday' else 'month-end / special day'}: wider band)"
    med, lo, hi = _band(values, k, floor)
    lo = max(lo, 0.0) if min(values) >= 0 else lo
    state = "high" if value > hi else "low" if value < lo else "normal"
    return {"state": state, "median": round(med, 2), "low": round(lo, 2), "high": round(hi, 2),
            "samples": len(values), "basis": basis}


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v) >= 10 or float(v).is_integer() else f"{v:.1f}"


def note(a: dict) -> str:
    if a["state"] == "learning":
        return ""
    rng = f"{_fmt(a['low'])}-{_fmt(a['high'])}"
    if a["state"] == "normal":
        return f"normal for {a['basis'].split(',')[0]} ({rng})"
    return f"UNUSUAL for {a['basis'].split(',')[0]}: normally {rng}"


# --------------------------------------------------------------- the sweep

def apply_to_metrics(system: str, metrics, when: datetime | None = None) -> list[dict]:
    """
    Annotate (and in smart mode re-grade) one sweep's graded metrics.
    Returns the per-metric assessments and stores the unusual ones for the wall.
    """
    m_ = mode()
    if m_ == "off":
        return []
    from core.models import Status
    when = when or datetime.now()
    data = _load_system(system)
    results, unusual = [], []
    for m in metrics:
        if not included(m.name) or not isinstance(m.value, (int, float)) or isinstance(m.value, bool):
            continue
        a = assess(system, m.name, float(m.value), when, data.get(m.name, []))
        a["metric"] = m.name
        a["value"] = m.value
        results.append(a)
        text = note(a)
        if not text:
            continue
        changed = ""
        if m_ == "smart" and m.status != Status.CRITICAL:
            if a["state"] == "normal" and m.status == Status.WARNING and not never_downgrade(m.name):
                m.status = Status.NORMAL
                changed = "; warning threshold crossed, but normal for this time -- graded NORMAL"
            elif a["state"] == "high" and m.status == Status.NORMAL:
                m.status = Status.WARNING
                changed = "; graded WARNING (unusual for this time)"
        m.detail = (f"{m.detail} · " if m.detail else "") + text + changed
        try:
            m.extra_data = dict(m.extra_data or {})
            m.extra_data["learned_baseline"] = {k: a[k] for k in ("state", "median", "low", "high", "basis")
                                                if k in a}
        except Exception:
            pass
        if a["state"] == "high":
            unusual.append({"metric": m.name, "value": m.value, "note": text, "graded": changed != ""})
    _store_flags(system, unusual, when)
    return results


def _flags_path(system: str) -> str:
    from utils.paths import BASE_DIR
    from reporting.system_paths import safe_system_name
    return os.path.join(BASE_DIR, "dashboard", "baseline_flags", f"{safe_system_name(system)}.json")


def _store_flags(system: str, unusual: list[dict], when: datetime) -> None:
    try:
        from core.atomic_json import read_json, write_json
        path = _flags_path(system)
        prev = read_json(path, default={}) or {}
        before = {u["metric"] for u in prev.get("unusual") or []}
        write_json(path, {"at": when.strftime("%Y-%m-%d %H:%M:%S"), "unusual": unusual})
        from core import timeline
        for u in unusual:
            if u["metric"] not in before:
                timeline.add(system, "baseline", f"{u['metric']} = {u['value']}: {u['note']}",
                             severity="warning")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"{system}: baseline flags not stored: {exc}")


def flags(system: str) -> dict:
    try:
        from core.atomic_json import read_json
        return read_json(_flags_path(system), default={}) or {}
    except Exception:
        return {}


def overview(system: str, current: dict[str, float] | None = None) -> list[dict]:
    """For the Operations page: every included metric with history, and its band now."""
    data = _load_system(system)
    now = datetime.now()
    out = []
    for metric in sorted(data):
        if not included(metric):
            continue
        series = data[metric]
        value = (current or {}).get(metric)
        if value is None and series:
            value = series[-1][1]
        a = assess(system, metric, float(value if value is not None else 0), now, series)
        a.update(metric=metric, value=value, days=len({t.date() for t, _ in series}))
        out.append(a)
    return out

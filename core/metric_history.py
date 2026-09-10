"""
Per-system metric history.

A point reading is often not enough to act on. "Memory is 99%" is ambiguous --
on a host where ASE preallocates its cache that is the steady state, and on a
host that read 70% last week it is an incident. The number is identical; only
the trend separates them.

WHY NOT THE EXISTING READER
reporting/history_reader.py reads the per-day Excel workbooks, and its
read_metric_history() takes no system argument -- the API passes system_name
and it is silently discarded, so every system's values land in one series.
A PRD memory trend containing QAS readings is worse than no trend, because it
looks authoritative. It also depends on a sweep having run, and PRD has had
none.

THIS STORE
One JSONL file per system per metric-month. Append-only, one line per sample,
no dependency on Excel or on sweeps. Written from the live RFC path, so
history accumulates from the moment the system is registered.

SAMPLING
The wall polls every 10 seconds. Recording that would be ~8,600 points per
metric per day to describe a value that moves slowly, so appends are throttled
to one sample per metric per interval (default 5 minutes). Trends are for
answering "is this getting worse over days", not for replaying seconds.

UNKNOWN IS RECORDED, NOT SKIPPED
A gap in a series is ambiguous -- it could mean the collector was down, or the
system was, or nothing was wrong. Unreadable samples are written with a null
value and a status, so a trend can show that a metric went dark rather than
implying continuity across a blind period.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from utils.paths import BASE_DIR

HISTORY_DIR = os.path.join(BASE_DIR, "dashboard", "history")

# One sample per metric per this many seconds.
SAMPLE_INTERVAL_SECONDS = 300

# Files are rotated monthly and pruned beyond this. Twelve months of a
# slow-moving metric is a few hundred KB; keeping it is cheaper than
# discovering a year later that the baseline was thrown away.
RETENTION_MONTHS = 12

_lock = threading.Lock()
_last_write: dict[tuple[str, str], datetime] = {}


def _safe(name: str) -> str:
    """Filesystem-safe component. Metric names contain dots and slashes."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]


def _path(system: str, when: datetime) -> str:
    return os.path.join(HISTORY_DIR, _safe(system),
                        f"{when.strftime('%Y-%m')}.jsonl")


def append_metrics(system: str, metrics, when: datetime | None = None,
                   force: bool = False) -> int:
    """
    Record the current value of each metric. Returns how many were written.

    Throttled per (system, metric): a metric already sampled inside
    SAMPLE_INTERVAL_SECONDS is skipped unless force=True. Never raises --
    losing a history sample must not take a monitoring read down with it.
    """
    when = when or datetime.now()
    written = 0

    try:
        with _lock:
            due = []
            for metric in metrics:
                key = (system, metric.name)
                last = _last_write.get(key)
                if not force and last and (when - last).total_seconds() < SAMPLE_INTERVAL_SECONDS:
                    continue
                due.append(metric)
                _last_write[key] = when

            if not due:
                return 0

            path = _path(system, when)
            os.makedirs(os.path.dirname(path), exist_ok=True)

            with open(path, "a", encoding="utf-8") as handle:
                for metric in due:
                    handle.write(json.dumps({
                        "t": when.isoformat(timespec="seconds"),
                        "m": metric.name,
                        # null for an unreadable metric, with the status kept
                        # so a gap can be told apart from a zero.
                        "v": metric.value,
                        "s": getattr(metric.status, "value", str(metric.status)),
                        "src": metric.source or "",
                    }, separators=(",", ":")) + "\n")
                    written += 1
    except Exception:
        return 0

    return written


def read_series(system: str, metric: str, days: int = 30) -> list[dict]:
    """Samples for one metric on one system, oldest first."""
    cutoff = datetime.now() - timedelta(days=days)
    points: list[dict] = []

    months = set()
    cursor = cutoff.replace(day=1)
    end = datetime.now()
    while cursor <= end:
        months.add(cursor.strftime("%Y-%m"))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    folder = os.path.join(HISTORY_DIR, _safe(system))
    if not os.path.isdir(folder):
        return []

    for month in sorted(months):
        path = os.path.join(folder, f"{month}.jsonl")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line or f'"{metric}"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("m") != metric:
                        continue
                    try:
                        stamp = datetime.fromisoformat(row["t"])
                    except (ValueError, KeyError):
                        continue
                    if stamp < cutoff:
                        continue
                    points.append({"timestamp": row["t"], "value": row.get("v"),
                                   "status": row.get("s"), "source": row.get("src", "")})
        except Exception:
            continue

    points.sort(key=lambda p: p["timestamp"])
    return points


def _mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def trend(system: str, metric: str, days: int = 7) -> dict:
    """
    Compare the most recent reading against the same metric `days` ago.

    Returns direction, the change, and how much data backs it. `confident` is
    False when the window holds too few samples -- a trend drawn from two
    points is a line, not evidence, and presenting it as one invites acting
    on noise.
    """
    series = read_series(system, metric, days=days + 1)
    readable = [p for p in series if isinstance(p["value"], (int, float))]

    if not readable:
        return {"metric": metric, "available": False,
                "reason": "no readable samples recorded yet"}

    current = readable[-1]["value"]
    now = datetime.now()
    window_start = now - timedelta(days=days)
    window_end = window_start + timedelta(hours=12)

    baseline_points = [
        p["value"] for p in readable
        if window_start <= datetime.fromisoformat(p["timestamp"]) <= window_end
    ]
    baseline = _mean(baseline_points)

    if baseline is None:
        # Not enough history yet. Say so plainly rather than comparing against
        # the oldest sample available and calling it "7 days".
        oldest = datetime.fromisoformat(readable[0]["timestamp"])
        return {"metric": metric, "available": False, "current": current,
                "samples": len(readable),
                "reason": f"history only goes back to {oldest:%Y-%m-%d %H:%M}"}

    change = current - baseline
    pct = (change / baseline * 100.0) if baseline else None
    unknown = sum(1 for p in series if p["value"] is None)

    return {
        "metric": metric,
        "available": True,
        "current": round(current, 2),
        "baseline": round(baseline, 2),
        "days": days,
        "change": round(change, 2),
        "change_percent": round(pct, 1) if pct is not None else None,
        "direction": "up" if change > 0 else "down" if change < 0 else "flat",
        "samples": len(readable),
        "baseline_samples": len(baseline_points),
        "unknown_samples": unknown,
        # Two points describe a line, not a trend. Note this measures the
        # BASELINE window, not the whole series: 200 samples this week and one
        # from the comparison day is a confident-looking number resting on a
        # single reading.
        "confident": len(readable) >= 6 and len(baseline_points) >= 3,
        "sparkline": [p["value"] for p in readable[-40:]],
    }


def prune(months: int = RETENTION_MONTHS) -> int:
    """Delete history files older than the retention window."""
    if not os.path.isdir(HISTORY_DIR):
        return 0
    cutoff = (datetime.now() - timedelta(days=31 * months)).strftime("%Y-%m")
    removed = 0
    for system in os.listdir(HISTORY_DIR):
        folder = os.path.join(HISTORY_DIR, system)
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if name.endswith(".jsonl") and name[:-6] < cutoff:
                try:
                    os.remove(os.path.join(folder, name))
                    removed += 1
                except OSError:
                    pass
    return removed

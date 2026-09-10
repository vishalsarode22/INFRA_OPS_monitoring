"""
Per-system monitoring schedules.

WHY PER SYSTEM
--------------
One global interval forces every system onto the same cadence. In practice
they are not equal: a production system may want a check every 15 minutes,
a sandbox once a day, and a system behind a fragile SAProuter perhaps only
during working hours. Sweeping all of them every two hours is either too
often for the quiet ones or too rare for the important one.

MODES
-----
    minutes   every N minutes
    hours     every N hours
    daily     once a day at HH:MM, optionally only on chosen weekdays

Schedules live in config/schedules.json so they survive a restart, unlike
the pause button which is deliberately temporary.

DESIGN NOTE
-----------
This module only ANSWERS "is this system due?". It never runs anything and
never touches SAP. Keeping the decision separate from the execution means it
can be unit-tested against a fixed clock, which matters for a component whose
bugs would otherwise only appear hours later at 3am.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__, "application")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
_PATH = os.path.join(_CONFIG_DIR, "schedules.json")
_lock = threading.Lock()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT = {
    "enabled": True,
    "mode": "hours",
    "every": 2,
    "at": "08:00",
    "days": list(WEEKDAYS),
}


def _normalise(entry: dict) -> dict:
    """Coerces a stored or submitted entry into a valid schedule."""
    out = dict(DEFAULT)
    out.update({k: v for k, v in (entry or {}).items() if k in DEFAULT})

    mode = str(out.get("mode", "hours")).strip().lower()
    out["mode"] = mode if mode in ("minutes", "hours", "daily") else "hours"

    try:
        every = int(out.get("every", 2))
    except (TypeError, ValueError):
        every = 2
    # A 1-minute sweep would start the next run before the previous finished:
    # a full cycle drives SAP GUI and takes minutes. Floor at 5.
    floor = 5 if out["mode"] == "minutes" else 1
    out["every"] = max(floor, every)

    # Out-of-range values are REJECTED, not wrapped. Taking 99:99 modulo
    # would silently produce 03:39 -- a valid-looking time nobody asked for,
    # and a schedule that fires in the middle of the night for no visible
    # reason. Falling back to the default is at least explainable.
    at = str(out.get("at", "08:00")).strip()
    try:
        hh, mm = (int(x) for x in at.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(at)
        out["at"] = f"{hh:02d}:{mm:02d}"
    except (ValueError, AttributeError, TypeError):
        log.warning(f"Invalid schedule time {at!r}; using {DEFAULT['at']}.")
        out["at"] = DEFAULT["at"]

    days = out.get("days") or list(WEEKDAYS)
    if isinstance(days, str):
        days = [d.strip().lower() for d in days.split(",")]
    days = [d for d in days if d in WEEKDAYS]
    out["days"] = days or list(WEEKDAYS)

    out["enabled"] = bool(out.get("enabled", True))
    return out


def load_schedules() -> dict:
    """Returns {system_name: schedule}. Missing file is not an error."""
    with _lock:
        if not os.path.exists(_PATH):
            return {}
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt schedule file must not stop monitoring entirely --
            # fall back to defaults rather than raising into the scheduler.
            log.error(f"Could not read {_PATH}: {exc}. Using defaults.")
            return {}
    return {name: _normalise(cfg) for name, cfg in raw.items()}


def save_schedules(schedules: dict) -> dict:
    cleaned = {name: _normalise(cfg) for name, cfg in (schedules or {}).items()}
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with _lock:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, indent=2)
    log.info(f"Saved schedules for {len(cleaned)} system(s).")
    return cleaned


def get_schedule(system: str) -> dict:
    return load_schedules().get(system, dict(DEFAULT))


def set_schedule(system: str, cfg: dict) -> dict:
    schedules = load_schedules()
    schedules[system] = _normalise(cfg)
    save_schedules(schedules)
    return schedules[system]


def describe(cfg: dict) -> str:
    """Human wording for the UI and the log."""
    cfg = _normalise(cfg)
    if not cfg["enabled"]:
        return "paused"
    if cfg["mode"] == "minutes":
        return f"every {cfg['every']} min"
    if cfg["mode"] == "hours":
        return f"every {cfg['every']} h"
    days = cfg["days"]
    when = "daily" if len(days) == 7 else ", ".join(d.title() for d in days)
    return f"{when} at {cfg['at']}"


def next_run_at(cfg: dict, last_run: datetime | None, now: datetime | None = None) -> datetime | None:
    """
    When this system should next be monitored. None when disabled.

    `last_run` is the start of its previous cycle; None means it has never
    run, in which case an interval schedule is due immediately and a daily
    schedule waits for its next slot.
    """
    cfg = _normalise(cfg)
    if not cfg["enabled"]:
        return None

    now = now or datetime.now()

    if cfg["mode"] in ("minutes", "hours"):
        if last_run is None:
            return now
        delta = (timedelta(minutes=cfg["every"]) if cfg["mode"] == "minutes"
                 else timedelta(hours=cfg["every"]))
        scheduled = last_run + delta
        # CATCH-UP GUARD. If a profile has been idle far longer than its
        # interval -- the server was off, monitoring was paused for a day,
        # a manual run held the slot -- `last_run + delta` is deep in the
        # past, so the profile is "due" and fires the instant monitoring is
        # free. That is how an unrequested PRD sweep followed an unrelated
        # manual run. When the scheduled time is more than one whole interval
        # behind now, treat the interval as restarting from now instead of
        # replaying every missed slot: the next run is one interval ahead,
        # not immediately.
        if scheduled < now - delta:
            return now + delta
        return scheduled

    hh, mm = (int(x) for x in cfg["at"].split(":"))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # Already run today at or after the slot -> look at tomorrow.
    if last_run and last_run >= candidate:
        candidate += timedelta(days=1)
    elif candidate < now and not (last_run and last_run >= candidate):
        # The slot passed today and we did not run: catch up tomorrow rather
        # than firing immediately, which would surprise an operator who
        # enabled the schedule in the afternoon.
        candidate += timedelta(days=1)

    for _ in range(8):
        if WEEKDAYS[candidate.weekday()] in cfg["days"]:
            return candidate
        candidate += timedelta(days=1)
    return None


def is_due(cfg: dict, last_run: datetime | None, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    nxt = next_run_at(cfg, last_run, now)
    return nxt is not None and nxt <= now

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
    minutes   every N minutes, on a fixed grid through `at`
    hours     every N hours, on a fixed grid through `at`
    daily     once a day at HH:MM
    `days` (weekdays) applies to every mode: a slot on a day that is not
    ticked does not exist.

SLOTS, NOT "LAST RUN + INTERVAL"
--------------------------------
Every schedule is a set of planned SLOTS on a fixed wall-clock grid:

    daily  at 07:00                  -> 07:00 every ticked day
    hours  every 2, at 08:00         -> ... 06:00 08:00 10:00 ... (ticked days)
    minutes every 15, at 08:05       -> ... 07:50 08:05 08:20 ...

The grid never moves: not with a restart, not with a manual run, not with
how long a sweep took. (It used to be "last run + interval", re-stamped at
the END of each sweep and re-anchored to "now" after a gap, so the times
drifted and a restart made overdue profiles fire within ~20 s.)

For each schedule the scheduler remembers up to which moment slots are
HANDLED (run, skipped or recorded as missed). A slot is

    due     it has passed, is not handled, and is at most grace() old;
    missed  it has passed, is not handled, and is older than grace()
            -- recorded once (run history, log) and never run late;
    upcoming otherwise.

A schedule with no recorded state (never run, or just saved) treats "now"
as handled: it waits for its next slot instead of firing at once.

grace() is IBO_SCHEDULE_GRACE_MINUTES (default 60, the window the owner
chose so two systems planned for the same minute can run one after the
other on the single SAP GUI desktop).

TIME ZONES
----------
Slots are computed in local wall-clock time (08:00 stays 08:00 across a DST
change). Stamps are STORED as ISO-8601 with the UTC offset; old stamps
without an offset are read as local time. The public functions accept naive
(local) or aware datetimes and return naive local times, which is what the
dashboard and its JSON have always used.

Schedules live in config/schedules.json (the store with the default
IBO_STATE_BACKEND=sqlite) so they survive a restart, unlike the pause button
which is deliberately temporary.

DESIGN NOTE
-----------
This module only ANSWERS "is this system due?". It never runs anything and
never touches SAP. Keeping the decision separate from the execution means it
can be unit-tested against a fixed clock, which matters for a component whose
bugs would otherwise only appear hours later at 3am.
"""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__, "application")

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
_PATH = os.path.join(_CONFIG_DIR, "schedules.json")
_lock = threading.Lock()

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Fixed origin of every interval grid (a Monday). Slots are origin-at-`at`
# plus whole intervals, so the grid is the same every day and never moves.
_GRID_ORIGIN = datetime(2000, 1, 3)


def grace() -> timedelta:
    """How late a slot may still start (IBO_SCHEDULE_GRACE_MINUTES, default 60)."""
    try:
        minutes = int(float(os.getenv("IBO_SCHEDULE_GRACE_MINUTES", "60") or 60))
    except (TypeError, ValueError):
        minutes = 60
    return timedelta(minutes=max(1, minutes))


# Kept for callers that read the constant; grace() is what decides.
DAILY_GRACE = timedelta(hours=1)

DEFAULT = {
    "enabled": True,
    "mode": "hours",
    "every": 2,
    "at": "08:00",
    "days": list(WEEKDAYS),
    "name": "",          # what people call this monitoring, e.g. "Full sweep" (28.10.2026)
}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def to_local_naive(dt: datetime | None) -> datetime | None:
    """Aware -> local wall-clock naive; naive is taken as local already."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def iso_stamp(dt: datetime) -> str:
    """ISO-8601 WITH the local UTC offset (naive input is local time)."""
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive = local
    return dt.isoformat(timespec="seconds")


def parse_stamp(value) -> datetime | None:
    """A stored stamp as naive local time; old offset-less stamps are local."""
    if not value:
        return None
    try:
        return to_local_naive(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _now(now: datetime | None) -> datetime:
    return to_local_naive(now) if now is not None else datetime.now()


# ---------------------------------------------------------------------------
# Schedule entries
# ---------------------------------------------------------------------------

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
    import re as _re
    out["name"] = _re.sub(r"[\x00-\x1f<>]", "", str(out.get("name") or "")).strip()[:60]
    return out


def load_schedules() -> dict:
    """
    Returns {system_name: schedule}. Missing schedules are not an error.

    Read through utils.atomic.read_json -- the SAME layer save_schedules()
    writes through. With the default IBO_STATE_BACKEND=sqlite that is the
    database (core/store.py), which imports config/schedules.json once on
    first use.
    """
    from utils.atomic import read_json
    with _lock:
        try:
            raw = read_json(_PATH, {}) or {}
        except Exception as exc:  # noqa: BLE001
            # A corrupt schedule store must not stop monitoring entirely --
            # fall back to "nothing scheduled" rather than raising.
            log.error(f"Could not read schedules: {exc}. Treating as none.")
            return {}
    if not isinstance(raw, dict):
        log.error("Schedules are not a mapping; treating as none.")
        return {}
    return {name: _normalise(cfg) for name, cfg in raw.items()
            if isinstance(cfg, dict) and not str(name).startswith("_")}


def save_schedules(schedules: dict) -> dict:
    cleaned = {name: _normalise(cfg) for name, cfg in (schedules or {}).items()}
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    with _lock:
        from utils.atomic import atomic_write_json
        atomic_write_json(_PATH, cleaned)
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
    days = cfg["days"]
    on = "" if len(days) == 7 else " on " + ", ".join(d.title() for d in days)
    if cfg["mode"] == "minutes":
        return f"every {cfg['every']} min (grid through {cfg['at']}){on}"
    if cfg["mode"] == "hours":
        return f"every {cfg['every']} h (grid through {cfg['at']}){on}"
    when = "daily" if len(days) == 7 else ", ".join(d.title() for d in days)
    return f"{when} at {cfg['at']}"


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def interval_delta(cfg: dict) -> timedelta | None:
    cfg = _normalise(cfg)
    if cfg["mode"] == "minutes":
        return timedelta(minutes=cfg["every"])
    if cfg["mode"] == "hours":
        return timedelta(hours=cfg["every"])
    return None


def _allowed(cfg: dict, slot: datetime) -> bool:
    return WEEKDAYS[slot.weekday()] in cfg["days"]


def previous_slot(cfg: dict, now: datetime | None = None) -> datetime | None:
    """The latest planned slot at or before `now` (naive local), or None."""
    cfg = _normalise(cfg)
    now = _now(now)
    hh, mm = (int(x) for x in cfg["at"].split(":"))
    delta = interval_delta(cfg)
    if delta is None:
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(days=1)
        for _ in range(8):
            if _allowed(cfg, slot):
                return slot
            slot -= timedelta(days=1)
        return None
    # Grid anchored on `at` EVERY day: every N through 08:00 gives the same
    # times each day, also when N does not divide 24 h (every 5 h: 03:00,
    # 08:00, 13:00, 18:00, 23:00). It used to run from a fixed origin in
    # 2000 and drift day to day (review 28.10.2026).
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(9):
        if _allowed(cfg, day):
            past = [t for t in _day_slots(day, hh, mm, delta) if t <= now]
            if past:
                return past[-1]
        day -= timedelta(days=1)
        now = day + timedelta(days=1) - timedelta(microseconds=1)
    return None


def next_slot(cfg: dict, now: datetime | None = None) -> datetime | None:
    """The earliest planned slot strictly after `now` (naive local), or None."""
    cfg = _normalise(cfg)
    now = _now(now)
    hh, mm = (int(x) for x in cfg["at"].split(":"))
    delta = interval_delta(cfg)
    if delta is None:
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now:
            slot += timedelta(days=1)
        for _ in range(8):
            if _allowed(cfg, slot):
                return slot
            slot += timedelta(days=1)
        return None
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(9):
        if _allowed(cfg, day):
            later = [t for t in _day_slots(day, hh, mm, delta) if t > now]
            if later:
                return later[0]
        day += timedelta(days=1)
        now = day - timedelta(microseconds=1)
    return None


def _day_slots(day: datetime, hh: int, mm: int, delta: timedelta) -> list[datetime]:
    """All slots of one calendar day: `at` plus/minus whole intervals, inside the day."""
    anchor = day.replace(hour=hh, minute=mm)
    first = anchor - ((anchor - day) // delta) * delta
    out, t = [], first
    end = day + timedelta(days=1)
    while t < end:
        out.append(t)
        t += delta
    return out


def slot_decision(cfg: dict, handled: datetime | None, now: datetime | None = None,
                  window: timedelta | None = None) -> tuple[str, datetime | None]:
    """
    ("disabled", None) | ("due", slot) | ("missed", slot) | ("wait", next_slot).

    `handled` is the moment up to which slots are dealt with (last planned
    run, a skip, a recorded miss, or the time the schedule was first seen).
    None is treated as `now`: nothing old is replayed.
    """
    cfg = _normalise(cfg)
    if not cfg["enabled"]:
        return "disabled", None
    now = _now(now)
    handled = to_local_naive(handled) if handled is not None else now
    window = window if window is not None else grace()
    prev = previous_slot(cfg, now)
    if prev is not None and prev > handled:
        if now - prev <= window:
            return "due", prev
        return "missed", prev
    return "wait", next_slot(cfg, now)


def next_run_at(cfg: dict, last_run: datetime | None, now: datetime | None = None) -> datetime | None:
    """
    When this schedule should next run. None when disabled.

    `last_run` is the moment up to which slots are handled (normally the
    planned slot of the last run). None -- never run -- waits for the next
    slot; it no longer fires immediately.
    A due slot is returned as it is (<= now); a missed one is skipped.
    """
    state, slot = slot_decision(cfg, last_run, now)
    if state == "disabled":
        return None
    if state == "due":
        return slot
    if state == "missed":
        return next_slot(cfg, now)
    return slot


def is_due(cfg: dict, last_run: datetime | None, now: datetime | None = None) -> bool:
    now = _now(now)
    nxt = next_run_at(cfg, last_run, now)
    return nxt is not None and nxt <= now


# ---------------------------------------------------------------------------
# Running the Scheduler page's schedules
# ---------------------------------------------------------------------------
#
# The per-system schedules on the Scheduler page are executed by the
# dashboard's scheduler loop (dashboard/app.py), alongside the Profiles page.
# A due system gets a full sweep -- the same as "Monitor now" on its card.
#
# Only a schedule someone SAVED can run. A system that was never saved shows
# "not scheduled" and never runs: DEFAULT exists to pre-fill the form, not to
# start sweeps nobody asked for.
#
# State per system (config/schedule_state.json, the store by default):
#     systems  {name: planned slot of its last scheduled run}
#     handled  {name: moment up to which slots are dealt with -- a skip, a
#               recorded miss, a save, a manual run}

STATE_PATH = os.path.join(_CONFIG_DIR, "schedule_state.json")
_state_lock = threading.Lock()
_last_good: dict = {}


def _load_full_state() -> dict:
    """{"systems": {...}, "handled": {...}}, with a last-good fallback when
    the stored state cannot be read (a torn or corrupt file must not make
    every schedule look never-run)."""
    from utils.atomic import read_json
    missing = object()
    try:
        data = read_json(STATE_PATH, missing)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Could not read schedule run state: {exc}")
        data = missing
    if data is missing or not isinstance(data, dict):
        good = _last_good.get(STATE_PATH)
        if good is not None and (data is not missing or os.path.exists(STATE_PATH)):
            log.warning("Schedule run state unreadable; using the last good copy.")
        return copy.deepcopy(good) if good is not None else {"systems": {}, "handled": {}}
    out = {"systems": dict(data.get("systems") or {}) if isinstance(data.get("systems"), dict) else {},
           "handled": dict(data.get("handled") or {}) if isinstance(data.get("handled"), dict) else {}}
    _last_good[STATE_PATH] = copy.deepcopy(out)
    return out


def _save_full_state(state: dict) -> None:
    from utils.atomic import atomic_write_json
    atomic_write_json(STATE_PATH, state)
    _last_good[STATE_PATH] = copy.deepcopy(state)


def _load_state() -> dict:
    """{system: last scheduled run stamp} (kept for older callers)."""
    return _load_full_state()["systems"]


def last_run(system: str) -> datetime | None:
    return parse_stamp(_load_state().get(system))


def mark_run(system: str, when: datetime | None = None) -> None:
    """Record a scheduled run of `system` for the slot `when` (default now)."""
    with _state_lock:
        state = _load_full_state()
        state["systems"][system] = iso_stamp(when or datetime.now())
        _save_full_state(state)


def set_handled(system: str, when: datetime | None = None) -> None:
    """Slots up to `when` are dealt with (skipped, missed, saved, manual run)."""
    with _state_lock:
        state = _load_full_state()
        state["handled"][system] = iso_stamp(when or datetime.now())
        _save_full_state(state)


def handled_until(system: str) -> datetime | None:
    state = _load_full_state()
    stamps = [parse_stamp(state["systems"].get(system)), parse_stamp(state["handled"].get(system))]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def stale_interval(cfg: dict, last: datetime | None, now: datetime,
                   never_run_is_stale: bool = True) -> bool:
    """
    LEGACY helper (core/schedule_notice.py still asks it): True for an
    every-N schedule whose last run is more than one interval behind.
    The scheduler itself no longer re-anchors anything -- slots are fixed.
    """
    cfg = _normalise(cfg)
    if not cfg["enabled"] or cfg["mode"] not in ("minutes", "hours"):
        return False
    if last is None:
        return never_run_is_stale
    delta = interval_delta(cfg)
    return to_local_naive(last) + delta < _now(now) - delta


def planned_next(cfg: dict | None, last: datetime | None,
                 now: datetime | None = None) -> datetime | None:
    """Next run of a saved system schedule, as the scheduler will treat it."""
    if not cfg:
        return None
    return next_run_at(cfg, last, now)


_missed_reported: set = set()


def missed_daily_slot(cfg: dict, last: datetime | None,
                      now: datetime | None = None) -> datetime | None:
    """The slot that was MISSED (passed by more than grace() without being
    handled), or None. Any mode. Never starts anything."""
    if last is None:
        return None
    state, slot = slot_decision(cfg, last, now)
    return slot if state == "missed" else None


def report_missed_once(key: str, slot: datetime, what: str) -> bool:
    """Log a missed slot once per key and slot. True the first time."""
    tag = (key, slot)
    if tag in _missed_reported:
        return False
    _missed_reported.add(tag)
    log.warning(f"Scheduled sweep MISSED: {what} was due at {slot:%Y-%m-%d %H:%M} and could "
                f"not start within {int(grace().total_seconds() // 60)} min (another "
                f"sweep was running, monitoring was paused, or the VM was off or busy). "
                f"It is not run late; the next slot is kept.")
    return True


def busy_window(busy_since: datetime | None, now: datetime | None = None) -> timedelta:
    """
    How late a slot may start when the dashboard was BUSY (a sweep or an RCA
    holding SAP GUI) since `busy_since`: the grace window plus the busy time,
    at most IBO_SCHEDULE_BUSY_MAX_MINUTES (default 360). A daily 09:00 run that
    waited behind a long RCA used to be "missed" and skipped for the day.
    """
    now = _now(now)
    base = grace()
    if busy_since is None:
        return base
    try:
        cap = int(float(os.getenv("IBO_SCHEDULE_BUSY_MAX_MINUTES", "360") or 360))
    except (TypeError, ValueError):
        cap = 360
    extra = max(timedelta(0), now - to_local_naive(busy_since))
    return min(base + extra, max(base, timedelta(minutes=cap)))


def due_systems_with_slots(system_names: list[str], now: datetime | None = None,
                           on_missed=None, window: timedelta | None = None) -> list[tuple[str, datetime]]:
    """
    (system, planned slot) for each SAVED schedule that is due now, in the
    given order.

    A schedule with no state yet starts counting now (waits for its next
    slot). A slot missed by more than grace() is recorded as handled,
    reported once (log, and on_missed(name, slot) if given) and never run.
    """
    now = _now(now)
    stored = load_schedules()
    due = []
    for name in system_names:
        cfg = stored.get(name)
        if not cfg or not cfg["enabled"]:
            continue
        handled = handled_until(name)
        if handled is None:
            set_handled(name, now)
            log.info(f"Schedule for {name} ({describe(cfg)}): first seen -- waits for its next slot.")
            continue
        state, slot = slot_decision(cfg, handled, now, window)
        if state == "missed":
            set_handled(name, slot)
            if report_missed_once("system:" + name, slot, name) and on_missed:
                try:
                    on_missed(name, slot)
                except Exception:  # noqa: BLE001 -- reporting must not stop scheduling
                    pass
        elif state == "due":
            due.append((name, slot))
    return due


def due_systems(system_names: list[str], now: datetime | None = None,
                on_missed=None) -> list[str]:
    """Names of the systems whose saved schedule is due now (see above)."""
    return [name for name, _slot in due_systems_with_slots(system_names, now, on_missed)]


def startup_pending(system_names: list[str], now: datetime | None = None) -> list[tuple[str, str, datetime]]:
    """
    At scheduler start: (system, "due"|"missed", slot) for every saved
    schedule whose most recent slot passed while the dashboard was not
    running. The caller decides (dashboard/app._apply_startup_policy) and records it.
    Schedules never seen before are given "now" as their start.
    """
    now = _now(now)
    stored = load_schedules()
    out = []
    for name in system_names:
        cfg = stored.get(name)
        if not cfg or not cfg["enabled"]:
            continue
        handled = handled_until(name)
        if handled is None:
            set_handled(name, now)
            continue
        state, slot = slot_decision(cfg, handled, now)
        if state in ("due", "missed"):
            out.append((name, state, slot))
    return out

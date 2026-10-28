"""
The name people gave a monitoring run, for the Monitoring log, Run history
and the next-runs table (owner, 28.10.2026: "give some name to monitoring so I
can tell the 1-hour check from the full sweep").

    profile run          -> the profile's name (Profiles page), e.g. "S4P quick check"
    Scheduler-page run   -> the schedule's name (Scheduler page), e.g. "Full sweep"
    anything else        -> ""   (manual, RCA, retry: the trigger says enough)
"""
from __future__ import annotations

SCHEDULE_RUN_LABEL = "Scheduler page"
_SCHEDULED = ("schedule", "catch-up")


def maps() -> tuple[dict, dict]:
    """({profile id: name}, {system: schedule name}) -- read once per page."""
    profiles, schedules = {}, {}
    try:
        from core.profiles import load_profiles
        profiles = {p["id"]: (p.get("label") or p["id"]) for p in load_profiles()}
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.schedules import load_schedules
        schedules = {s: c.get("name") or "" for s, c in load_schedules().items()}
    except Exception:  # noqa: BLE001
        pass
    return profiles, schedules


def name_for(row: dict, names: tuple[dict, dict] | None = None) -> str:
    profiles, schedules = names or maps()
    pid = row.get("profile_id")
    if pid and pid != SCHEDULE_RUN_LABEL:
        return profiles.get(pid) or str(pid)
    if str(row.get("trigger") or "") in _SCHEDULED:
        return schedules.get(str(row.get("system") or "")) or ""
    return ""


def add_names(rows: list[dict]) -> list[dict]:
    names = maps()
    for r in rows:
        r["run_name"] = name_for(r, names)
    return rows

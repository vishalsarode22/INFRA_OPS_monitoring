"""
Review fix 4: after a restart, a slot still inside the grace window runs once
("catch-up") by default -- also with MONITOR_ON_STARTUP=false, the value
.env.example shipped -- and only slots older than the grace window are
recorded as missed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_fix_scheduler_slots import _profile, at, env, sch  # noqa: E402,F401


def test_daily_0700_slot_runs_after_a_restart_at_0710(env, monkeypatch):
    import dashboard.app as app
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles", lambda: [_profile(at="07:00")])
    monkeypatch.setattr(app, "get_systems", lambda: [])
    profiles.mark_run("p1", at(7, 0, days=-1))             # yesterday's run
    out = app._apply_startup_policy(now=at(7, 10))         # default
    assert [(o[1], o[3]) for o in out] == [("p1", "catch-up")]
    assert [p["id"] for p in profiles.due_profiles(at(7, 10, 20))] == ["p1"]
    assert app._trigger_once("profile", "p1") == "catch-up"


def test_slot_older_than_grace_is_recorded_missed(env, monkeypatch):
    import dashboard.app as app
    from core import profiles, run_history
    monkeypatch.setattr(profiles, "load_profiles", lambda: [_profile(at="07:00")])
    monkeypatch.setattr(app, "get_systems", lambda: [])
    profiles.mark_run("p1", at(7, 0, days=-1))
    out = app._apply_startup_policy(now=at(8, 30))         # 90 min late, grace 60
    assert [(o[1], o[3]) for o in out] == [("p1", "missed")]
    assert profiles.due_profiles(at(8, 30, 20)) == []
    assert run_history.query(result="missed")


def test_system_schedule_catches_up_too(env, monkeypatch):
    import dashboard.app as app
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles", lambda: [])
    monkeypatch.setattr(app, "get_systems", lambda: [{"name": "PRD"}])
    sch.set_schedule("PRD", {"enabled": True, "mode": "daily", "every": 1, "at": "07:00",
                              "days": list(sch.WEEKDAYS)})
    sch.mark_run("PRD", at(7, 0, days=-1))
    out = app._apply_startup_policy(now=at(7, 10))
    assert [(o[2], o[3]) for o in out] == [("PRD", "catch-up")]


def test_loop_applies_catch_up_even_with_monitor_on_startup_false(env, monkeypatch):
    import dashboard.app as app
    seen = []
    monkeypatch.setenv("MONITOR_ON_STARTUP", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setattr(app, "_apply_startup_policy", lambda run=True, now=None: seen.append(run) or [])

    class _Stop(Exception):
        pass

    def _stop():
        raise _Stop()
    monkeypatch.setattr(app, "_scheduler_pass", _stop)
    try:
        app._scheduler_loop()
    except _Stop:
        pass
    assert seen == [True]


def test_env_example_documents_the_rule():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, ".env.example"), encoding="utf-8").read()
    assert "catch-up" in text and "\nMONITOR_ON_STARTUP=false" not in text

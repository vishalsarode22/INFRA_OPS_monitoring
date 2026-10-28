"""
Scheduler slot logic (findings 1-4, 13 of the 23 Oct 2026 review).

Fixed clocks only; the real config and database are never touched.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import schedules as sch  # noqa: E402

DAY = datetime(2026, 10, 9)          # a Friday


def at(hh, mm=0, ss=0, days=0):
    return DAY + timedelta(days=days, hours=hh, minutes=mm, seconds=ss)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    monkeypatch.delenv("IBO_SCHEDULE_GRACE_MINUTES", raising=False)
    monkeypatch.setattr(sch, "_PATH", str(tmp_path / "schedules.json"))
    monkeypatch.setattr(sch, "STATE_PATH", str(tmp_path / "schedule_state.json"))
    sch._missed_reported.clear()
    sch._last_good.clear()
    from core import profiles, run_history
    monkeypatch.setattr(profiles, "STATE_PATH", str(tmp_path / "profile_state.json"))
    profiles._last_good.clear()
    monkeypatch.setattr(profiles, "resolve_tcodes", lambda p: ["SM51"])
    monkeypatch.setattr(run_history, "DB_PATH", str(tmp_path / "rh.sqlite"))
    return tmp_path


def _profile(pid="p1", **sched):
    s = {"enabled": True, "mode": "daily", "every": 1, "at": "07:00",
         "days": list(sch.WEEKDAYS)}
    s.update(sched)
    return {"id": pid, "system": "PRD", "deliver": ["email"], "label": pid,
            "schedule": sch._normalise(s)}


# ---------------------------------------------------------------- finding 1

def test_daily_profile_fires_on_a_20s_poll_after_its_slot(env, monkeypatch):
    """The slot is hit by a pass at 07:00:15, not only at exactly 07:00:00."""
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles", lambda: [_profile()])
    assert profiles.due_profiles(at(6, 0)) == []          # first seen at 06:00
    fired = None
    for k in range(0, 200):                                # 20 s passes, 06:00 -> 07:06
        now = at(6, 0) + timedelta(seconds=20 * k + 15)
        due = profiles.due_profiles(now)
        if due:
            fired = now
            assert due[0]["planned_at"] == at(7, 0)
            profiles.mark_run(due[0]["id"], due[0]["planned_at"])
            break
    assert fired is not None and at(7, 0) <= fired < at(7, 0, 20)
    assert profiles.due_profiles(at(7, 1)) == []           # once a day
    assert [p["id"] for p in profiles.due_profiles(at(7, 0, 15, days=1))] == ["p1"]


def test_daily_slot_past_the_grace_is_recorded_missed_and_next_is_kept(env, monkeypatch):
    from core import profiles, run_history
    monkeypatch.setenv("IBO_SCHEDULE_GRACE_MINUTES", "30")
    monkeypatch.setattr(profiles, "load_profiles", lambda: [_profile()])
    profiles.mark_run("p1", at(7, 0, days=-1))
    assert profiles.due_profiles(at(7, 29)) != []          # inside 30 min
    assert profiles.due_profiles(at(7, 31)) == []          # missed
    assert profiles.due_profiles(at(9, 0)) == []           # recorded once, not replayed
    rows = run_history.query(result="missed")
    assert len(rows) == 1 and rows[0]["system"] == "PRD"
    assert rows[0]["profile_id"] == "p1" and rows[0]["planned_at"].startswith("2026-10-09T07:00:00")
    st = profiles.status(at(9, 0))[0]
    assert st["next_run"] == "2026-10-10T07:00:00"


def test_grace_is_configurable(env, monkeypatch):
    monkeypatch.setenv("IBO_SCHEDULE_GRACE_MINUTES", "5")
    cfg = {"enabled": True, "mode": "daily", "at": "08:00"}
    assert sch.slot_decision(cfg, at(7), at(8, 5)) == ("due", at(8))
    assert sch.slot_decision(cfg, at(7), at(8, 6)) == ("missed", at(8))
    monkeypatch.delenv("IBO_SCHEDULE_GRACE_MINUTES")
    assert sch.grace() == timedelta(minutes=60)


# ---------------------------------------------------------------- finding 2

def test_overdue_interval_next_run_is_stable_across_calls_and_arrives():
    cfg = {"enabled": True, "mode": "hours", "every": 2, "at": "08:00"}
    last = at(8, 0, days=-3)
    seen = {sch.next_run_at(cfg, last, at(9, 30) + timedelta(seconds=20 * k)) for k in range(30)}
    assert seen == {at(10)}                    # not "now + delta", which moved with the clock
    assert sch.is_due(cfg, last, at(10, 0, 20))


def test_interval_grid_does_not_drift_with_sweep_duration():
    cfg = {"enabled": True, "mode": "minutes", "every": 30, "at": "08:05"}
    assert sch.next_slot(cfg, at(9, 0)) == at(9, 5)
    # the 09:05 run took 25 min; the next slot is still 09:35
    assert sch.next_run_at(cfg, at(9, 5), at(9, 30)) == at(9, 35)


# ---------------------------------------------------------------- finding 3

def test_never_run_interval_profile_does_not_fire_immediately(env, monkeypatch):
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [_profile(mode="hours", every=2, at="08:00")])
    assert profiles.due_profiles(at(9, 17)) == []
    assert profiles.due_profiles(at(9, 59)) == []
    assert [p["id"] for p in profiles.due_profiles(at(10, 0, 20))] == ["p1"]


def test_startup_policy_without_catch_up_records_missed_and_waits(env, monkeypatch):
    import dashboard.app as app
    from core import profiles, run_history
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [_profile(mode="hours", every=2, at="08:00")])
    monkeypatch.setattr(app, "get_systems", lambda: [])
    monkeypatch.setattr(app, "next_runs", lambda now=None: {"systems": {}})
    profiles.mark_run("p1", at(8))                         # ran 08:00, server down 09:00-10:20
    out = app._apply_startup_policy(False, now=at(10, 20))
    assert [(o[1], o[3]) for o in out] == [("p1", "missed")]
    assert profiles.due_profiles(at(10, 20, 20)) == []     # nothing fires after the restart
    assert [p["id"] for p in profiles.due_profiles(at(12, 0, 20))] == ["p1"]
    rows = run_history.query(result="missed")
    assert rows and "catch-up disabled" in (rows[0]["failure_reason"] or "")


def test_startup_policy_true_runs_one_catch_up(env, monkeypatch):
    import dashboard.app as app
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [_profile(mode="hours", every=2, at="08:00")])
    monkeypatch.setattr(app, "get_systems", lambda: [])
    profiles.mark_run("p1", at(8))
    out = app._apply_startup_policy(True, now=at(10, 20))
    assert [(o[1], o[3]) for o in out] == [("p1", "catch-up")]
    assert app._trigger_once("profile", "p1") == "catch-up"
    assert app._trigger_once("profile", "p1") == "schedule"


def test_scheduler_pass_stamps_the_planned_slot_and_records_the_trigger(env, monkeypatch):
    import dashboard.app as app
    from core import profiles
    monkeypatch.setattr(profiles, "load_profiles",
                        lambda: [_profile(mode="hours", every=2, at="08:00")])
    profiles.set_handled("p1", at(9))
    started = []
    monkeypatch.setattr(app, "_run_one_system_background",
                        lambda system, **kw: started.append((system, kw)) or True)
    monkeypatch.setattr(app, "_sweep_block_reason", lambda s: None)
    monkeypatch.setattr(app, "_retention_tick", lambda: None)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(10, 0, 40)
    monkeypatch.setattr(profiles, "datetime", _Clock)
    monkeypatch.setattr(sch, "datetime", _Clock)
    monkeypatch.setattr(app, "datetime", _Clock)
    app._scheduler_state.update(enabled=True, snooze_until=None, manual_pause=False)
    app._run_state["running"] = False
    app._scheduler_pass()
    assert started == [("PRD", {"profile_id": "p1", "trigger": "schedule", "planned_at": at(10)})]
    assert profiles.last_run("p1") == at(10)               # the slot, not the finish time
    started.clear()
    app._scheduler_pass()
    assert started == []                                   # not due again


# ---------------------------------------------------------------- finding 4

def test_days_and_at_apply_to_interval_modes():
    cfg = {"enabled": True, "mode": "hours", "every": 3, "at": "07:30",
           "days": ["mon", "tue", "wed", "thu", "fri"]}
    assert sch.next_slot(cfg, at(8)) == at(10, 30)          # grid through 07:30
    assert sch.next_slot(cfg, at(23)) == at(1, 30, days=3)  # Fri 23:00 -> Mon 01:30
    assert sch.previous_slot(cfg, at(12, days=1)) == at(22, 30)   # Saturday -> Fri 22:30
    assert sch.slot_decision(cfg, at(22, 30), at(12, days=1))[0] == "wait"
    assert "Mon" in sch.describe(cfg) and "07:30" in sch.describe(cfg)


def test_minutes_mode_weekday_filter():
    cfg = {"enabled": True, "mode": "minutes", "every": 15, "at": "00:00", "days": ["sun"]}
    assert sch.next_slot(cfg, at(10)) == at(0, 0, days=2)   # Fri -> Sun 00:00


# ---------------------------------------------------------------- finding 13

def test_stamps_are_stored_with_offset_and_old_naive_stamps_read_as_local(env):
    sch.mark_run("BW", at(8))
    raw = json.loads(open(sch.STATE_PATH, encoding="utf-8").read())
    stamp = raw["systems"]["BW"]
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert sch.last_run("BW") == at(8)
    # an old offset-less stamp is local time
    with open(sch.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"systems": {"BW": "2026-10-09T08:00:00"}}, fh)
    sch._last_good.clear()
    assert sch.last_run("BW") == at(8)


def test_aware_now_is_accepted():
    cfg = {"enabled": True, "mode": "daily", "at": "08:00"}
    aware = at(8, 10).astimezone()                          # local, with offset
    assert sch.slot_decision(cfg, at(7), aware) == ("due", at(8))
    utc = aware.astimezone(timezone.utc)
    assert sch.slot_decision(cfg, at(7), utc) == ("due", at(8))

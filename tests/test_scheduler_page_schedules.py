"""
Scheduler page schedules: saved, shown and run (09 Oct 2026 fix).

Before the fix:
  * a save went into the database (utils.atomic -> core/store.py) while the
    page read config/schedules.json, so a refresh showed "every 2 h" again
    and each save replaced the previous one;
  * the scheduler never ran these schedules at all (profiles only);
  * profile last-run times were read from the file, never from the database
    they were written to;
  * an every-N-hours schedule idle for more than one interval had its next
    run pushed forward on every pass, so it never ran.

Runs with plain `python -m unittest` (no pytest needed) and under pytest.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Env(unittest.TestCase):
    """Temp database + temp schedule files; the real config is never touched."""

    backend = "sqlite"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ibo_sched_")
        self._env = {k: os.environ.get(k) for k in ("IBO_STATE_BACKEND", "IBO_DB_PATH")}
        os.environ["IBO_STATE_BACKEND"] = self.backend

        from core import store
        self.store = store
        self._db = store.DB_PATH
        store.DB_PATH = os.path.join(self.tmp, "test.sqlite")
        if hasattr(store, "_local") and hasattr(store._local, "conn"):
            del store._local.conn
        store._initialised = False

        from core import schedules
        self.sch = schedules
        self._paths = (schedules._PATH, schedules.STATE_PATH)
        schedules._PATH = os.path.join(self.tmp, "schedules.json")
        schedules.STATE_PATH = os.path.join(self.tmp, "schedule_state.json")

    def tearDown(self):
        self.sch._PATH, self.sch.STATE_PATH = self._paths
        if hasattr(self.store._local, "conn"):
            try:
                self.store._local.conn.close()
            except Exception:
                pass
            del self.store._local.conn
        self.store.DB_PATH = self._db
        self.store._initialised = False
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class SaveAndLoad(_Env):

    def test_save_is_what_load_returns_and_saves_accumulate(self):
        # The old file, as on the VM: four systems, all paused.
        old = {n: {"enabled": False, "mode": "hours", "every": 2}
               for n in ("CENTOR_QAS", "PRD", "CARFOUR QAS", "PS4")}
        with open(self.sch._PATH, "w", encoding="utf-8") as fh:
            json.dump(old, fh)

        self.sch.set_schedule("CEAT BW_PRD", {"enabled": True, "mode": "daily", "at": "08:00"})
        self.sch.set_schedule("CEAT FIORI_PRD", {"enabled": True, "mode": "daily", "at": "09:00"})
        self.sch.set_schedule("CEAT GRC_PRD", {"enabled": True, "mode": "daily", "at": "10:00"})

        got = self.sch.load_schedules()
        self.assertEqual(got["CEAT BW_PRD"]["mode"], "daily")
        self.assertEqual(got["CEAT BW_PRD"]["at"], "08:00")
        self.assertEqual(got["CEAT FIORI_PRD"]["at"], "09:00")
        self.assertEqual(got["CEAT GRC_PRD"]["at"], "10:00")
        # the old entries survived the import and the saves
        self.assertFalse(got["PS4"]["enabled"])
        self.assertEqual(len(got), 7)

    def test_no_schedules_anywhere_is_empty(self):
        self.assertEqual(self.sch.load_schedules(), {})


class SaveAndLoadFilesBackend(SaveAndLoad):
    backend = "files"


class SystemSchedules(_Env):

    def setUp(self):
        super().setUp()
        self.day = datetime(2026, 10, 9)          # a Friday

    def at(self, hh, mm=0, days=0):
        return self.day + timedelta(days=days, hours=hh, minutes=mm)

    def test_unsaved_or_disabled_systems_never_run(self):
        self.sch.set_schedule("OFF", {"enabled": False, "mode": "minutes", "every": 5})
        for h in range(24):
            self.assertEqual(self.sch.due_systems(["NEVER SAVED", "OFF"], self.at(h)), [])
        self.assertIsNone(self.sch.planned_next(None, None, self.at(9)))
        self.assertIsNone(self.sch.planned_next(self.sch.load_schedules()["OFF"], None, self.at(9)))

    def test_daily_runs_once_at_its_time(self):
        self.sch.set_schedule("BW", {"enabled": True, "mode": "daily", "at": "08:00"})
        self.sch.mark_run("BW", self.at(7, 50))             # saved at 07:50
        self.assertEqual(self.sch.due_systems(["BW"], self.at(7, 59)), [])
        self.assertEqual(self.sch.due_systems(["BW"], self.at(8, 0)), ["BW"])
        self.sch.mark_run("BW", self.at(8, 0))              # scheduler starts it
        self.sch.mark_run("BW", self.at(8, 9))              # ... and it ends
        for t in (self.at(8, 10), self.at(12), self.at(23, 59)):
            self.assertEqual(self.sch.due_systems(["BW"], t), [])
        self.assertEqual(self.sch.due_systems(["BW"], self.at(8, 0, days=1)), ["BW"])

    def test_daily_saved_after_its_time_waits_for_tomorrow(self):
        self.sch.set_schedule("GRC", {"enabled": True, "mode": "daily", "at": "10:00"})
        self.sch.mark_run("GRC", self.at(11, 30))           # saved at 11:30
        self.assertEqual(self.sch.due_systems(["GRC"], self.at(11, 31)), [])
        self.assertEqual(
            self.sch.planned_next(self.sch.load_schedules()["GRC"], self.at(11, 30), self.at(11, 31)),
            self.at(10, 0, days=1))

    def test_daily_respects_weekdays(self):
        self.sch.set_schedule("WK", {"enabled": True, "mode": "daily", "at": "08:00",
                                     "days": ["mon", "tue", "wed", "thu", "fri"]})
        self.sch.mark_run("WK", self.at(7))
        self.assertEqual(self.sch.due_systems(["WK"], self.at(8, 0, days=1)), [])   # Saturday
        # Friday run, then Monday 08:00
        self.sch.mark_run("WK", self.at(8, 5))
        self.assertEqual(self.sch.due_systems(["WK"], self.at(8, 0, days=3)), ["WK"])

    # 23 Oct 2026: interval schedules run on a FIXED grid through `at`
    # (default 08:00 -> 08:00, 10:00, 12:00 ...), stamped with the planned
    # slot. They used to count from the end of the last sweep and re-anchor
    # to "now" after a gap, so the times drifted with every run and restart.

    def test_every_2h_runs_on_its_grid_and_repeats(self):
        self.sch.set_schedule("FG", {"enabled": True, "mode": "hours", "every": 2})
        self.sch.mark_run("FG", self.at(11, 30))            # saved at 11:30
        self.assertEqual(self.sch.due_systems(["FG"], self.at(11, 59)), [])
        self.assertEqual(self.sch.due_systems_with_slots(["FG"], self.at(12, 0, )),
                         [("FG", self.at(12))])
        self.sch.mark_run("FG", self.at(12, 0))             # scheduler stamps the SLOT
        # however long the sweep took, the next slot is 14:00
        self.assertEqual(self.sch.due_systems(["FG"], self.at(13, 59)), [])
        self.assertEqual(self.sch.due_systems(["FG"], self.at(14, 0)), ["FG"])

    def test_every_2h_after_long_gap_runs_on_the_next_grid_slot(self):
        """The old bug: next run moved ahead on every pass and never came."""
        self.sch.set_schedule("FG", {"enabled": True, "mode": "hours", "every": 2})
        self.sch.mark_run("FG", self.at(8, 0, days=-3))     # last run 3 days ago
        t = self.at(9, 1)                                   # 08:00 slot is 61 min old
        self.assertEqual(self.sch.due_systems(["FG"], t), [])     # missed, not run late
        ran = None
        for minutes in range(0, 4 * 60, 1):                       # 20 s passes, roughly
            now = t + timedelta(minutes=minutes)
            if self.sch.due_systems(["FG"], now):
                ran = now
                break
        self.assertEqual(ran, self.at(10))

    def test_never_run_interval_waits_one_interval(self):
        # e.g. a schedule saved before this fix (no run state yet)
        self.sch.set_schedule("X", {"enabled": True, "mode": "hours", "every": 2})
        self.assertIsNone(self.sch.last_run("X"))
        self.assertEqual(self.sch.due_systems(["X"], self.at(9)), [])
        self.assertEqual(self.sch.due_systems(["X"], self.at(11)), ["X"])

    def test_same_time_systems_queue_instead_of_skipping_a_day(self):
        """BW and MDG both at 08:00: MDG runs when BW finishes (late, same day)."""
        for n in ("BW", "MDG"):
            self.sch.set_schedule(n, {"enabled": True, "mode": "daily", "at": "08:00"})
            self.sch.mark_run(n, self.at(7))
        self.assertEqual(self.sch.due_systems(["BW", "MDG"], self.at(8)), ["BW", "MDG"])
        self.sch.mark_run("BW", self.at(8, 25))             # BW swept 08:00-08:25
        self.assertEqual(self.sch.due_systems(["BW", "MDG"], self.at(8, 25)), ["MDG"])

    def test_daily_more_than_one_hour_late_waits_for_tomorrow(self):
        self.sch.set_schedule("BW", {"enabled": True, "mode": "daily", "at": "08:00"})
        self.sch.mark_run("BW", self.at(7))
        self.assertEqual(self.sch.due_systems(["BW"], self.at(9, 0)), ["BW"])    # exactly 1 h
        self.assertEqual(self.sch.due_systems(["BW"], self.at(9, 1)), [])        # VM was off
        self.assertEqual(
            self.sch.planned_next(self.sch.load_schedules()["BW"], self.at(7), self.at(9, 1)),
            self.at(8, 0, days=1))

    def test_missed_daily_slot_is_reported_once_and_not_run(self):
        self.sch._missed_reported.clear()
        self.sch.set_schedule("BW", {"enabled": True, "mode": "daily", "at": "08:00"})
        self.sch.mark_run("BW", self.at(7))
        seen = []
        for t in (self.at(9, 30), self.at(9, 31), self.at(15)):
            self.assertEqual(self.sch.due_systems(["BW"], t, on_missed=lambda n, s: seen.append((n, s))), [])
        self.assertEqual(seen, [("BW", self.at(8))])
        # on time the next day: runs, nothing reported
        self.assertEqual(self.sch.due_systems(["BW"], self.at(8, 0, days=1),
                                              on_missed=lambda n, s: seen.append((n, s))), ["BW"])
        self.assertEqual(len(seen), 1)

    def test_one_order_for_several_due(self):
        for n in ("A", "B"):
            self.sch.set_schedule(n, {"enabled": True, "mode": "daily", "at": "08:00"})
            self.sch.mark_run(n, self.at(7))
        self.assertEqual(self.sch.due_systems(["B", "A"], self.at(8)), ["B", "A"])


class ProfileState(_Env):

    def setUp(self):
        super().setUp()
        from core import profiles
        self.pr = profiles
        self._state = profiles.STATE_PATH
        profiles.STATE_PATH = os.path.join(self.tmp, "profile_state.json")
        self._load = profiles.load_profiles
        self._resolve = profiles.resolve_tcodes
        profiles.resolve_tcodes = lambda p: ["SM51"]

    def tearDown(self):
        self.pr.STATE_PATH = self._state
        self.pr.load_profiles = self._load
        self.pr.resolve_tcodes = self._resolve
        super().tearDown()

    def test_last_run_is_read_from_where_it_is_written(self):
        with open(self.pr.STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"profiles": {"prd-quick": "2026-09-15T14:24:34"}, "alerts": {}}, fh)
        self.assertEqual(self.pr.last_run("prd-quick"), datetime(2026, 9, 15, 14, 24, 34))
        self.pr.mark_run("prd-quick", datetime(2026, 10, 9, 9, 0))
        self.assertEqual(self.pr.last_run("prd-quick"), datetime(2026, 10, 9, 9, 0))

    def test_stale_every_2h_profile_runs_on_its_next_grid_slot(self):
        with open(self.pr.STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"profiles": {"prd-quick": "2026-09-15T14:24:34"}, "alerts": {}}, fh)
        self.pr.load_profiles = lambda: [{
            "id": "prd-quick", "system": "PRD", "deliver": ["whatsapp"],
            "schedule": {"enabled": True, "mode": "hours", "every": 2,
                         "at": "08:00", "days": list(self.sch.WEEKDAYS)}}]
        t = datetime(2026, 10, 9, 11, 30)                   # 10:00 slot 90 min old
        self.assertEqual(self.pr.due_profiles(t), [])                 # missed, not run late
        self.assertEqual(self.pr.due_profiles(t + timedelta(minutes=29)), [])
        due = self.pr.due_profiles(datetime(2026, 10, 9, 12, 0, 20))
        self.assertEqual([p["id"] for p in due], ["prd-quick"])
        self.assertEqual(due[0]["planned_at"], datetime(2026, 10, 9, 12, 0))

    def test_never_run_profile_waits_for_its_next_slot(self):
        """It used to fire on the first pass after it was created."""
        self.pr.load_profiles = lambda: [{
            "id": "new", "system": "PRD", "deliver": [],
            "schedule": {"enabled": True, "mode": "hours", "every": 2,
                         "at": "08:00", "days": list(self.sch.WEEKDAYS)}}]
        self.assertEqual(self.pr.due_profiles(datetime(2026, 10, 9, 9)), [])
        self.assertEqual(self.pr.due_profiles(datetime(2026, 10, 9, 9, 59)), [])
        self.assertEqual([p["id"] for p in self.pr.due_profiles(datetime(2026, 10, 9, 10, 0, 20))],
                         ["new"])

    def test_daily_profile_unchanged(self):
        self.pr.load_profiles = lambda: [{
            "id": "prd-full", "system": "PRD", "deliver": ["email"],
            "schedule": {"enabled": True, "mode": "daily", "every": 1,
                         "at": "07:00", "days": list(self.sch.WEEKDAYS)}}]
        self.assertEqual(self.pr.due_profiles(datetime(2026, 10, 9, 6, 59)), [])
        self.pr.mark_run("prd-full", datetime(2026, 10, 8, 7, 5))
        self.assertEqual([p["id"] for p in self.pr.due_profiles(datetime(2026, 10, 9, 7, 0))],
                         ["prd-full"])
        # blocked at 07:00 by another sweep until 07:20: still runs today
        self.assertEqual([p["id"] for p in self.pr.due_profiles(datetime(2026, 10, 9, 7, 20))],
                         ["prd-full"])
        # a profile that never ran keeps waiting for its next slot
        self.pr.load_profiles = lambda: [{
            "id": "fresh", "system": "PRD", "deliver": [],
            "schedule": {"enabled": True, "mode": "daily", "every": 1,
                         "at": "07:00", "days": list(self.sch.WEEKDAYS)}}]
        self.assertEqual(self.pr.due_profiles(datetime(2026, 10, 9, 7, 20)), [])


class ProfileStateFilesBackend(ProfileState):
    backend = "files"


if __name__ == "__main__":
    unittest.main()

"""27 Oct 2026: an hourly profile (CEAT S4P prd-quick) -- what can break it."""
import json
import os
from datetime import datetime

from core import run_history
from test_fix_monitoring_log import env  # noqa: F401


def _profile(pid, system, slot):
    return {"id": pid, "system": system, "planned_at": slot, "resolved_tcodes": ["SM58"], "deliver": "email"}


def test_oldest_slot_first_and_a_lost_start_is_due_again(env, monkeypatch):  # noqa: F811
    app, _a, _o = env
    import core.profiles as pr
    calls = []
    daily = _profile("s4q-daily", "CEAT S4_QAS", datetime(2026, 10, 27, 9, 0))
    hourly = _profile("prd-quick", "CEAT S4P", datetime(2026, 10, 27, 10, 0))
    monkeypatch.setattr(pr, "due_profiles", lambda now=None, window=None: [hourly, daily])
    saved = {}
    monkeypatch.setattr(pr, "stamps", lambda pid: ("2026-10-27T08:00:00", None))
    monkeypatch.setattr(pr, "restore_stamps", lambda pid, s: saved.update({pid: s}))
    monkeypatch.setattr(pr, "mark_run", lambda pid, slot: calls.append(("mark", pid)))
    monkeypatch.setattr(app, "_run_one_system_background", lambda s, **k: calls.append(("run", s)) or False)
    monkeypatch.setattr(app, "_sweep_block_reason", lambda s: None)
    monkeypatch.setattr(app, "_stop_hold_reason", lambda s: None)
    app._retry_once.clear()
    app._scheduler_state.update(enabled=True, snooze_until=None, manual_pause=False)
    app._run_state["running"] = False
    app._scheduler_pass()
    assert calls[:2] == [("mark", "s4q-daily"), ("run", "CEAT S4_QAS")]      # daily slot waited longer
    assert saved == {"s4q-daily": ("2026-10-27T08:00:00", None)}               # not started -> due again
    saved.clear()
    app._scheduler_pass()
    assert saved == {}                                                          # only once per slot


def test_a_run_cut_off_by_a_restart_shows_as_failed(env, tmp_path, monkeypatch):  # noqa: F811
    app, _a, _o = env
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path))
    with open(tmp_path / "active_run.json", "w") as fh:
        json.dump({"system": "PS4", "profile_id": "prd-quick", "trigger": "schedule",
                   "planned_at": "2026-10-27T10:00:00", "started_at": "2026-10-27T10:00:05", "pid": 999999}, fh)
    app._recover_interrupted_run()
    rows = run_history.query(system="PS4")
    assert rows and rows[0]["result"] == "failed" and "dashboard stopped during this run" in rows[0]["failure_reason"]
    assert not os.path.exists(tmp_path / "active_run.json")
    app._active_run_mark("PS4", "x", "schedule", None)
    assert os.path.exists(tmp_path / "active_run.json")
    app._active_run_mark(None)
    assert not os.path.exists(tmp_path / "active_run.json")


def test_learned_baseline_read_beats_and_stops_at_its_budget(monkeypatch):
    import json as _json
    from core import learned_baseline as lb, atomic_json, heartbeat
    lb.forget()
    rows = (_json.dumps({"m": "x", "v": 1, "t": datetime.now().isoformat()}) for _ in range(20000))
    monkeypatch.setattr(atomic_json, "read_lines", lambda p: rows)
    beats = []
    monkeypatch.setattr(heartbeat, "beat", lambda stage=None: beats.append(stage))
    monkeypatch.setattr(lb, "_LOAD_BUDGET_S", 0.0)
    data = lb._load_system("CEAT S4P")
    assert beats and beats[0] == "CEAT S4P:learned-baseline"
    assert 0 < len(data.get("x", [])) < 20000          # stopped early, kept what it read


def test_an_interrupted_slot_runs_again_after_the_restart(env, tmp_path, monkeypatch):  # noqa: F811
    app, _a, _o = env
    import core.profiles as pr
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path))
    restored = {}
    monkeypatch.setattr(pr, "restore_stamps", lambda pid, st: restored.update({pid: st}))
    app._stamps_before["prd-quick"] = ["2026-10-28T11:00:00", None]
    app._active_run_mark("CEAT S4P", "prd-quick", "schedule", datetime(2026, 10, 28, 12, 0))
    data = json.load(open(tmp_path / "active_run.json"))
    data["pid"] = 999999                                # written by the process that died
    json.dump(data, open(tmp_path / "active_run.json", "w"))
    app._recover_interrupted_run()
    assert restored == {"prd-quick": ("2026-10-28T11:00:00", None)}


def test_no_new_sweep_while_a_stop_is_pending(env, tmp_path, monkeypatch):  # noqa: F811
    app, _a, _o = env
    import core.profiles as pr
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "stop_pending").write_text("x")
    called = []
    monkeypatch.setattr(pr, "due_profiles", lambda now=None, window=None: called.append(1) or [])
    app._scheduler_state.update(enabled=True, snooze_until=None, manual_pause=False)
    app._run_state["running"] = False
    app._scheduler_pass()
    assert called == []


def test_a_run_of_another_live_dashboard_is_left_alone(env, tmp_path, monkeypatch):  # noqa: F811
    """28.10: a second dashboard marked the first one's live run failed and re-ran the slot."""
    app, _a, _o = env
    import core.profiles as pr
    monkeypatch.setenv("IBO_RUNTIME_DIR", str(tmp_path))
    restored = {}
    monkeypatch.setattr(pr, "restore_stamps", lambda pid, st: restored.update({pid: st}))
    import subprocess, sys
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with open(tmp_path / "active_run.json", "w") as fh:
            json.dump({"system": "CEAT S4P", "profile_id": "prd-quick", "trigger": "schedule",
                       "planned_at": "2026-10-28T14:00:00", "started_at": "2026-10-28T14:22:00",
                       "pid": other.pid, "stamps_before": ["2026-10-28T13:00:00", None]}, fh)
        app._recover_interrupted_run()
        assert os.path.exists(tmp_path / "active_run.json")       # marker kept
        assert restored == {}                                     # slot NOT put back
        assert not [r for r in run_history.query(system="CEAT S4P") if r.get("planned_at", "").startswith("2026-10-28T14")]
    finally:
        other.kill(); other.wait()
    assert app._pid_alive(other.pid) is False
    assert app._pid_alive(0) is False

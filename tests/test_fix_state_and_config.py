"""
State files and config loading (findings 5, 9, 10 of the 23 Oct 2026 review).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- finding 5

def test_atomic_write_retries_a_windows_sharing_violation(tmp_path, monkeypatch):
    from utils import atomic
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "being used by another process")
        return real(src, dst)
    monkeypatch.setattr(atomic.os, "replace", flaky)
    p = tmp_path / "x.yaml"
    atomic.atomic_write_yaml(str(p), {"systems": [{"name": "A"}]}, header="# h\n")
    assert calls["n"] == 3
    assert p.read_text(encoding="utf-8").startswith("# h\nsystems:")
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_atomic_write_locked_file_is_written_in_place(tmp_path, monkeypatch):
    # Windows refuses the swap while any process has the file open (every IBOPS
    # process reads systems.yaml); giving up made "Save system" fail on the VM
    # with WinError 5. A writable file is now overwritten in place instead.
    from utils import atomic
    p = tmp_path / "x.txt"
    p.write_text("old", encoding="utf-8")
    monkeypatch.setattr(atomic.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    atomic.atomic_write_text(str(p), "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_atomic_write_read_only_file_gives_up_and_leaves_the_old_file(tmp_path, monkeypatch):
    from utils import atomic
    p = tmp_path / "x.txt"
    p.write_text("old", encoding="utf-8")
    monkeypatch.setattr(atomic.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(atomic.os, "access", lambda path, mode: False)
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError, match="read-only"):
        atomic.atomic_write_text(str(p), "new")
    assert p.read_text(encoding="utf-8") == "old"
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


@pytest.fixture
def pstate(tmp_path, monkeypatch):
    from core import profiles
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    monkeypatch.setattr(profiles, "STATE_PATH", str(tmp_path / "profile_state.json"))
    profiles._last_good.clear()
    return profiles


def test_torn_profile_state_uses_last_good_and_keeps_alert_times(pstate):
    pr = pstate
    pr.mark_run("prd-quick", datetime(2026, 10, 9, 10, 0))
    pr.mark_alerted("PRD:CRITICAL", datetime(2026, 10, 9, 10, 5))
    # a half-written file, as a reader saw it mid-write before the fix
    with open(pr.STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write('{"profiles": {"prd-qu')
    assert pr.last_run("prd-quick") == datetime(2026, 10, 9, 10, 0)
    # a write on top of the torn read must not wipe the suppression times
    pr.mark_run("prd-full", datetime(2026, 10, 9, 11, 0))
    data = json.loads(open(pr.STATE_PATH, encoding="utf-8").read())
    assert "PRD:CRITICAL" in data["alerts"]
    assert "prd-quick" in data["profiles"] and "prd-full" in data["profiles"]


def test_torn_schedule_state_uses_last_good(tmp_path, monkeypatch):
    from core import schedules as sch
    monkeypatch.setenv("IBO_STATE_BACKEND", "files")
    monkeypatch.setattr(sch, "STATE_PATH", str(tmp_path / "schedule_state.json"))
    sch._last_good.clear()
    sch.mark_run("BW", datetime(2026, 10, 9, 8, 0))
    with open(sch.STATE_PATH, "w", encoding="utf-8") as fh:
        fh.write("{")
    assert sch.last_run("BW") == datetime(2026, 10, 9, 8, 0)


def test_profiles_yaml_is_written_atomically(tmp_path, monkeypatch):
    from core import profiles
    monkeypatch.setattr(profiles, "PROFILES_PATH", str(tmp_path / "monitoring_profiles.yaml"))
    import core.delivery_settings as ds
    monkeypatch.setattr(ds, "validate_recipients", lambda *a, **k: [])
    profiles.save_profiles([{"id": "p", "system": "PRD", "tcodes": "all", "deliver": ["email"],
                             "schedule": {"mode": "daily", "at": "07:00"}}], alerts={})
    text = open(profiles.PROFILES_PATH, encoding="utf-8").read()
    assert text.startswith("# Monitoring profiles")
    assert [p["id"] for p in profiles.load_profiles()] == ["p"]
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


# ---------------------------------------------------------------- findings 9, 10

@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    from core import config_loader as cl
    conf = tmp_path / "config"
    conf.mkdir()
    monkeypatch.setattr(cl, "CONFIG_DIR", str(conf))
    cl.invalidate_systems_cache()
    monkeypatch.setattr(cl, "_systems_good", None)
    cl._config_errors.clear()
    cl._env_seen["mtime"] = None
    try:
        from core import live_settings
        monkeypatch.setattr(live_settings, "sync", lambda: None)
    except Exception:
        pass
    monkeypatch.setattr(cl, "_scoped", lambda systems: systems)
    yield cl
    cl.invalidate_systems_cache()
    cl._config_errors.clear()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 2))      # make the mtime move


def test_malformed_systems_yaml_serves_last_good_and_reports_it(cfgdir):
    cl = cfgdir
    path = os.path.join(cl.CONFIG_DIR, "systems.yaml")
    _write(path, "systems:\n  - name: PRD\n    client: '100'\n")
    assert [s["name"] for s in cl.get_systems()] == ["PRD"]
    assert cl.config_error() is None
    _write(path, "systems:\n  - name: PRD\n\tclient: '100'\n")      # a TAB: ScannerError
    assert [s["name"] for s in cl.get_systems()] == ["PRD"]          # no exception, last good
    err = cl.config_error()
    assert err and err["file"] == "systems.yaml" and err["serving"] == "last good copy"
    _write(path, "systems:\n  - name: QAS\n    client: '200'\n")
    assert [s["name"] for s in cl.get_systems()] == ["QAS"]
    assert cl.config_error() is None


def test_malformed_systems_yaml_at_startup_gives_empty_list_not_500(cfgdir):
    cl = cfgdir
    _write(os.path.join(cl.CONFIG_DIR, "systems.yaml"), "systems: [unclosed\n")
    assert cl.get_systems() == []
    assert cl.config_error()["serving"] == "no systems"


def test_api_scheduler_exposes_config_error(cfgdir, monkeypatch):
    cl = cfgdir
    import dashboard.app as app
    _write(os.path.join(cl.CONFIG_DIR, "systems.yaml"), "systems: [unclosed\n")
    cl.get_systems()
    monkeypatch.setattr(app, "_enabled_profile_count", lambda: 0)
    monkeypatch.setattr(app, "_enabled_schedule_count", lambda: 0)
    out = app.scheduler_status()
    assert out["config_error"]["file"] == "systems.yaml"


def test_changed_env_value_is_picked_up_without_restart(cfgdir, monkeypatch):
    cl = cfgdir
    monkeypatch.setenv("IBO_SETTINGS_SYNC", "1")
    env_path = os.path.join(os.path.dirname(cl.CONFIG_DIR), ".env")
    _write(env_path, "ZZ_TEST_RFC_HOST=10.0.0.1\nZZ_UNRELATED=keep\n")
    monkeypatch.setenv("ZZ_TEST_RFC_HOST", "10.0.0.1")
    monkeypatch.setenv("ZZ_UNRELATED", "process-value")
    path = os.path.join(cl.CONFIG_DIR, "systems.yaml")
    _write(path, "systems:\n  - name: PRD\n    rfc:\n      ashost: ${ZZ_TEST_RFC_HOST}\n")
    assert cl.get_systems()[0]["rfc"]["ashost"] == "10.0.0.1"
    _write(env_path, "ZZ_TEST_RFC_HOST=10.0.0.2\nZZ_UNRELATED=from-file\n")
    assert cl.get_systems()[0]["rfc"]["ashost"] == "10.0.0.2"
    assert os.environ["ZZ_UNRELATED"] == "process-value"      # only referenced keys


def test_written_secret_updates_env_and_file_atomically(cfgdir, monkeypatch):
    cl = cfgdir
    env_path = os.path.join(os.path.dirname(cl.CONFIG_DIR), ".env")
    _write(env_path, "A=1\nZZ_SECRET_X=old\n")
    monkeypatch.setenv("ZZ_SECRET_X", "old")
    cl._write_env_secret("ZZ_SECRET_X", "new")
    assert os.environ["ZZ_SECRET_X"] == "new"
    assert "ZZ_SECRET_X=new" in open(env_path, encoding="utf-8").read()
    assert [f for f in os.listdir(os.path.dirname(env_path)) if f.endswith(".tmp")] == []

"""Fixes from the RFC / sweep concurrency simulation (Oct 2026)."""
import multiprocessing as mp
import os
import threading
import time

import pytest

import rfc_worker_helpers as H
from core import rfc_workers as W


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("IBO_RFC_PROCESSES", "2")
    W.shutdown()
    W._closed["on"] = False
    yield
    W.shutdown()
    W._closed["on"] = False


def test_second_read_of_a_busy_system_is_refused_and_holds_no_slot():
    W.run("OK", H.add, (0, 0), timeout=30)
    t = threading.Thread(target=lambda: _swallow(lambda: W.run("HUNG", H.hang, (3,), timeout=10)))
    t.start()
    time.sleep(0.8)
    with pytest.raises(W.RfcBusy):
        W.run("HUNG", H.add, (1, 1), timeout=10)          # refused at once, not queued
    # Two slots: one used by the hung read, the duplicates never took the other.
    for _ in range(5):
        assert W.run("OK", H.add, (2, 2), timeout=10) == 4
    t.join(15)


def _swallow(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


def test_worker_in_use_is_never_reaped(monkeypatch):
    monkeypatch.setenv("IBO_RFC_WORKER_IDLE_MINUTES", "1")
    t = threading.Thread(target=lambda: W.run("SLOW", H.hang, (2,), timeout=10))
    t.start()
    time.sleep(0.8)
    with W._workers_lock:
        W._workers["SLOW"].last_used -= 3600                 # looks idle by time...
    W.run("OTHER", H.add, (1, 1), timeout=10)               # ...a reap pass runs here
    assert "SLOW" in {r["system"] for r in W.status()}      # ...but it is in use: kept
    t.join(10)


def test_shutdown_refuses_new_reads():
    W.shutdown()
    with pytest.raises(W.RfcShuttingDown):
        W.run("X", H.add, (1, 1), timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="uses /proc to check the orphan")
def test_worker_exits_when_the_dashboard_dies(tmp_path):
    pidfile = str(tmp_path / "pid")
    ctx = mp.get_context("spawn")
    p = ctx.Process(target=H.spawn_worker_then_die, args=(str(tmp_path), pidfile))
    p.start()
    p.join(30)
    assert os.path.exists(pidfile)
    wpid = int(open(pidfile).read())
    deadline = time.time() + 20
    while time.time() < deadline and os.path.exists(f"/proc/{wpid}"):
        try:
            if open(f"/proc/{wpid}/stat").read().split()[2] == "Z":
                break
        except OSError:
            break
        time.sleep(0.5)
    alive = os.path.exists(f"/proc/{wpid}") and open(f"/proc/{wpid}/stat").read().split()[2] != "Z"
    assert not alive, "worker outlived the dashboard"


def test_dashboard_keeps_last_reading_when_busy_and_never_caches_none(monkeypatch):
    pytest.importorskip("fastapi")
    import dashboard.app as A
    A._live_cache["SYS"] = {"system": "SYS", "connected": True, "cpu": 5}
    monkeypatch.setattr(A, "_run_in_rfc_pool", lambda *a: (_ for _ in ()).throw(W.RfcBusy("busy")))
    A._refresh_one_live({"name": "SYS"})
    assert A._live_cache["SYS"]["cpu"] == 5                  # last reading kept
    monkeypatch.setattr(A, "_run_in_rfc_pool", lambda *a: None)
    A._refresh_one_live({"name": "SYS"})
    assert isinstance(A._live_cache["SYS"], dict) and A._live_cache["SYS"]["connected"] is False
    assert "no data" in A._live_cache["SYS"]["error"]


def test_wall_copy_is_trimmed_but_full_reading_reaches_alerts(monkeypatch):
    pytest.importorskip("fastapi")
    import dashboard.app as A
    big = {"system": "BIG", "connected": True, "dump_rows": list(range(50000)),
           "checks": {"rows": list(range(2000))}}
    seen = {}
    monkeypatch.setattr(A, "_run_in_rfc_pool", lambda *a: dict(big))
    monkeypatch.setattr(A, "_rca_observe", lambda name, p: seen.setdefault("rows", len(p["dump_rows"])))
    A._refresh_one_live({"name": "BIG"})
    cached = A._live_cache["BIG"]
    assert len(cached["dump_rows"]) == 500 and cached["dump_rows_truncated_from"] == 50000
    assert len(cached["checks"]["rows"]) == 500
    assert seen["rows"] == 50000                              # downstream saw everything


def test_rca_refuses_sap_gui_while_an_abandoned_sweep_is_alive(monkeypatch):
    pytest.importorskip("fastapi")
    import dashboard.app as A
    import main
    monkeypatch.setattr(main, "abandoned_gui_alive",
                        lambda: {"system": "HANGS_80", "run_id": "r1", "at": "now"})
    ran = []
    import evaluation.rca_pipeline as rp
    monkeypatch.setattr(rp, "run_rca", lambda *a, **k: ran.append(1) or {})
    monkeypatch.setattr(A, "get_systems", lambda: [{"name": "PS4"}])

    class T:
        cfg = type("C", (), {"preempt_running_sweep": False})()
    monkeypatch.setattr(A, "_rca_trigger", T())
    A._rca_cancel["on"] = False
    A._run_rca_background("PS4", "test")
    assert ran == [] and "abandoned" in A._rca_state["last"]["error"]
    assert A._run_state["running"] is False

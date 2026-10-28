"""
Sweeps were killed as "hung -- no progress for ~190s (last stage: analysis)"
after all screenshots were taken: the analysis stage waited silently on RFC
(extra checks up to 600 s, a full live read on the sweep thread). Those waits
now beat the heartbeat and are bounded.
"""
import threading
import time

import pytest

main = pytest.importorskip("main")


def test_wait_beating_beats_while_a_slow_step_works(monkeypatch):
    beats = []
    monkeypatch.setattr(main.heartbeat, "beat", lambda stage=None: beats.append(stage))
    monkeypatch.setattr(main.heartbeat, "cancelled", lambda: False)
    real_join = threading.Thread.join

    def fast_join(self, timeout=None):             # 20 s beats, compressed
        return real_join(self, None if timeout is None else min(timeout, 0.05))

    monkeypatch.setattr(threading.Thread, "join", fast_join)
    assert main._wait_beating(lambda: (time.sleep(0.4), "ok")[1], "S4Q:extra-checks", 30) == "ok"
    assert beats and set(beats) == {"S4Q:extra-checks"}


def test_wait_beating_gives_up_at_its_limit_and_raises_errors(monkeypatch):
    monkeypatch.setattr(main.heartbeat, "beat", lambda stage=None: None)
    monkeypatch.setattr(main.heartbeat, "cancelled", lambda: False)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        main._wait_beating(lambda: time.sleep(5), "x", 0.3)
    assert time.monotonic() - t0 < 2
    with pytest.raises(ValueError, match="boom"):
        main._wait_beating(lambda: (_ for _ in ()).throw(ValueError("boom")), "x", 5)


def test_live_detail_uses_the_dashboards_fresh_reading_without_rfc(monkeypatch):
    import sys
    import types
    fake = types.SimpleNamespace(_live_cache_lock=threading.Lock(),
                                 _live_cache={"S4Q": {"connected": True, "job_detail": {"J": 1}}},
                                 _live_cache_at={"S4Q": time.monotonic()})
    monkeypatch.setitem(sys.modules, "dashboard.app", fake)
    from core import rfc_workers
    monkeypatch.setattr(rfc_workers, "run", lambda *a, **k: pytest.fail("no RFC read expected"))
    assert main._live_payload_for_sweep("S4Q", {})["job_detail"] == {"J": 1}


def test_live_detail_timeout_returns_empty(monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "dashboard.app", raising=False)
    monkeypatch.setenv("IBO_RFC_PROCESSES", "2")
    monkeypatch.setattr(main, "_wait_beating", lambda fn, stage, limit: (_ for _ in ()).throw(TimeoutError()))
    assert main._live_payload_for_sweep("S4Q", {}) == {}

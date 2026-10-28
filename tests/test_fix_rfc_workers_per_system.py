"""
Each SAP system's live read runs in its own worker process
(core/rfc_workers.py). One system's crash or hang must not break or cancel
another system's read -- with one shared pool it did (BrokenProcessPool and
"cancelled twice by worker restarts" on every card).
"""
import threading
import time

import pytest

import rfc_worker_helpers as H
from core import rfc_workers as W


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("IBO_RFC_PROCESSES", "3")
    W.shutdown()
    W._closed["on"] = False          # shutdown() refuses new reads; tests reopen
    yield
    W.shutdown()
    W._closed["on"] = False


def test_each_system_gets_its_own_long_lived_process():
    a1 = W.run("PRD", H.pid, (), timeout=30)
    a2 = W.run("PRD", H.pid, (), timeout=30)
    b = W.run("CEAT S4P", H.pid, (), timeout=30)
    assert a1 == a2 and a1 != b
    assert W.run("PRD", H.add, (40, 2), timeout=30) == 42


def test_crash_in_one_system_does_not_touch_the_others():
    before = W.run("PS4", H.pid, (), timeout=30)
    with pytest.raises(W.RfcWorkerDied, match=r"CEAT S4P.*twice.*exit code 3"):
        W.run("CEAT S4P", H.crash, (), timeout=30)
    assert W.run("PS4", H.pid, (), timeout=30) == before       # same process, still fine


def test_crash_is_retried_once_on_a_fresh_process(tmp_path):
    assert W.run("CAR PRD", H.crash_once, (str(tmp_path / "m"),), timeout=30) == "recovered"


def test_hang_stops_only_that_systems_worker():
    other = W.run("PS4", H.pid, (), timeout=30)
    results = {}

    def slow():
        try:
            W.run("CEAT S4P", H.hang, (30,), timeout=1.5)
        except Exception as exc:  # noqa: BLE001
            results["s4p"] = exc

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.3)
    # While S4P hangs, PS4 reads normally, on its same process.
    assert W.run("PS4", H.add, (1, 1), timeout=30) == 2
    t.join(20)
    assert isinstance(results.get("s4p"), W.RfcCallTimeout)
    assert "Other systems were not affected" in str(results["s4p"])
    assert W.run("PS4", H.pid, (), timeout=30) == other
    assert W.run("CEAT S4P", H.add, (2, 3), timeout=30) == 5   # fresh worker afterwards


def test_parallel_limit_and_queue_wait_do_not_count_toward_the_timeout(monkeypatch):
    monkeypatch.setenv("IBO_RFC_PROCESSES", "1")
    W.shutdown()
    W._closed["on"] = False
    for name in ("A", "B", "C"):        # start the processes first (spawn takes a moment)
        W.run(name, H.add, (0, 0), timeout=30)
    errors, out = [], []

    def call(name):
        try:
            out.append(W.run(name, H.hang, (0.8,), timeout=1.5))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    ts = [threading.Thread(target=call, args=(n,)) for n in ("A", "B", "C")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    # One at a time: 3 x 0.8 s, far over 1.5 s in total, yet none timed out.
    assert errors == [] and out == ["late"] * 3


def test_errors_come_back_as_errors_even_when_they_do_not_pickle():
    with pytest.raises(ValueError, match="bad table"):
        W.run("PRD", H.raise_value, (), timeout=30)
    with pytest.raises(RuntimeError, match="OddError: Name or password is incorrect"):
        W.run("PRD", H.raise_odd, (), timeout=30)
    assert W.run("PRD", H.add, (1, 2), timeout=30) == 3          # worker survived


def test_exit_codes_are_explained():
    assert "access violation" in W.describe_exit(3221225477)
    assert "out of memory" in W.describe_exit(0xC0000017)
    assert W.describe_exit(None) == "exit code unknown"


def test_dashboard_records_a_double_crash_once_on_the_errors_page(monkeypatch):
    pytest.importorskip("fastapi")
    import dashboard.app as A
    from core import errors as E
    captured = []
    monkeypatch.setattr(E, "capture", lambda *a, **k: captured.append((a, k)))
    monkeypatch.setattr(A, "_rfc_call_timeout", lambda: 15)
    with pytest.raises(A.RfcWorkerDied):
        A._run_in_rfc_pool(H.crash)
    assert len(captured) == 0          # one crash is a warning; 3 in 6 h go to the Errors page
    assert A._run_in_rfc_pool(H.add, "PRD", "x") == "PRDx"


def test_idle_workers_are_stopped(monkeypatch):
    W.run("OLD", H.add, (1, 1), timeout=30)
    with W._workers_lock:
        W._workers["OLD"].last_used -= 3600
    W.run("NEW", H.add, (1, 1), timeout=30)
    assert [r["system"] for r in W.status()] == ["NEW"]

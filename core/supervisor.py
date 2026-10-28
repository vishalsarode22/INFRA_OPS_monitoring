"""
Supervised background loops: a loop that crashes (or returns when it should
run forever) is reported (core/errors.py, source "loop") and restarted.

    start("scheduler", _scheduler_loop)

Restart delay grows 10 s, 30 s, 60 s, 120 s, then stays at 300 s while it
keeps failing; a loop that has run for 10 minutes since its last start is
considered healthy again and the delay resets.

Every supervised loop's state (running / restarting, restarts, last error)
is written to the heartbeats table as service "loop:<name>" every 60 s, so
the Health page shows it from any process.
"""
from __future__ import annotations

import socket
import threading
import time

from utils.logger import get_logger

log = get_logger(__name__)

_loops: dict[str, dict] = {}
_lock = threading.Lock()
_monitor_started = False
_DELAYS = (10, 30, 60, 120, 300)


def _beat(name: str, st: dict) -> None:
    try:
        from core import store
        detail = f"{st['state']}; restarts {st['restarts']}"
        if st.get("last_error"):
            detail += f"; last error {st['last_error'][:90]}"
        store.beat(f"loop:{name}", detail)
    except Exception:
        pass


def _monitor() -> None:
    while True:
        with _lock:
            items = list(_loops.items())
        for name, st in items:
            _beat(name, st)
        time.sleep(60)


def start(name: str, target, *args, restart: bool = True, restart_on_return: bool = False,
          **kwargs) -> threading.Thread:
    """
    Run `target` on a daemon thread; restart it when it raises. A loop that
    RETURNS has stopped on purpose (e.g. SCHEDULER_ENABLED=false) and is left
    stopped, unless restart_on_return=True.
    """
    global _monitor_started
    st = {"state": "starting", "restarts": 0, "last_error": "", "started": time.monotonic(),
          "host": socket.gethostname()}
    with _lock:
        if name in _loops and _loops[name].get("thread") and _loops[name]["thread"].is_alive():
            return _loops[name]["thread"]
        _loops[name] = st
        if not _monitor_started:
            _monitor_started = True
            threading.Thread(target=_monitor, daemon=True, name="loop-monitor").start()

    def _run():
        failures = 0
        while True:
            st["state"] = "running"
            st["started"] = time.monotonic()
            _beat(name, st)
            try:
                target(*args, **kwargs)
                why = "returned (a forever-loop should not return)"
                exc = None
                if not restart_on_return:
                    st["state"] = "stopped (returned on purpose)"
                    _beat(name, st)
                    return
            except Exception as e:  # noqa: BLE001
                why = f"{type(e).__name__}: {e}"
                exc = e
            if not restart:
                st["state"] = "stopped"
                if exc is not None:
                    _report(name, exc, why)
                _beat(name, st)
                return
            if time.monotonic() - st["started"] > 600:
                failures = 0
            delay = _DELAYS[min(failures, len(_DELAYS) - 1)]
            failures += 1
            st.update(state=f"restarting in {delay}s", last_error=why[:300])
            st["restarts"] += 1
            log.error(f"background loop '{name}' stopped ({why}); restarting in {delay}s "
                      f"(restart {st['restarts']})")
            _report(name, exc, why)
            _beat(name, st)
            time.sleep(delay)

    t = threading.Thread(target=_run, daemon=True, name=f"loop-{name}")
    st["thread"] = t
    t.start()
    return t


def _report(name: str, exc, why: str) -> None:
    try:
        from core import errors
        if exc is not None:
            errors.capture(exc, "loop", context=f"background loop {name}")
        else:
            errors.capture(None, "loop", context=f"background loop {name}", etype="LoopStopped",
                           message=f"{name}: {why}", frames=[f"loop:{name}"])
    except Exception:
        pass


def status() -> dict[str, dict]:
    with _lock:
        return {n: {k: v for k, v in st.items() if k != "thread"} | {"alive": bool(st.get("thread") and
                                                                                  st["thread"].is_alive())}
                for n, st in _loops.items()}

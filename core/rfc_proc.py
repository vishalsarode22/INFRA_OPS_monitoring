"""
Run background RFC work (extra checks, change snapshots, the synthetic
logon) in ONE worker PROCESS instead of a thread of the web server.

On this pyrfc / NW RFC SDK build an RFC call holds the GIL for its whole
duration (see dashboard/app.py _get_rfc_pool): a slow destination test on a
thread of the web server would freeze every page. A separate process has
its own GIL. One worker, so these runs queue behind each other and the
worker's caches (function interfaces, table fields) stay warm.

    IBO_CHECK_PROCESSES=0    run in the calling thread instead (tests; or if
                             the worker process ever misbehaves)

The functions passed must be top-level and picklable (collectors/checks_job.py).
"""
from __future__ import annotations

import os
import threading

from utils.logger import get_logger

log = get_logger(__name__)

_pool = None
_lock = threading.Lock()
_inflight: list = []          # futures submitted and not finished


class WorkerBusy(RuntimeError):
    """The worker is still on earlier work and the caller asked not to queue."""


def busy() -> bool:
    with _lock:
        _inflight[:] = [f for f in _inflight if not f.done()]
        return bool(_inflight)


def enabled() -> bool:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return os.getenv("IBO_CHECK_PROCESSES", "1").strip().lower() not in ("0", "false", "no", "off")


def _get():
    global _pool
    with _lock:
        if _pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            # "spawn" on every OS: forking a multi-threaded web server can
            # copy a lock some other thread holds (logging, SQLite) and hang.
            _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        return _pool


def _reset() -> None:
    global _pool
    with _lock:
        old, _pool = _pool, None
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def call(fn, *args, timeout: float = 900, skip_if_busy: bool = False):
    """fn(*args) in the worker process (or here when disabled). Raises what fn raises;
    concurrent.futures.TimeoutError when the worker takes longer than
    `timeout` (it keeps running). skip_if_busy: raise WorkerBusy instead of
    queueing behind earlier work (for timed measurements)."""
    if not enabled():
        return fn(*args)
    from concurrent.futures.process import BrokenProcessPool
    if skip_if_busy and busy():
        raise WorkerBusy("background RFC worker is busy")
    try:
        fut = _get().submit(fn, *args)
        with _lock:
            _inflight.append(fut)
        return fut.result(timeout=timeout)
    except BrokenProcessPool:
        # The worker died (crash in the RFC SDK, killed). Start a new one next time.
        log.warning("background RFC worker process died -- restarting it")
        _reset()
        raise

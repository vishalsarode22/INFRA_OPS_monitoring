"""
One worker PROCESS per SAP system for live RFC reads.

Before: every system's live read went through ONE shared ProcessPoolExecutor
with 2 workers. When a read crashed its worker (a fault inside the NetWeaver
RFC SDK, Windows reclaiming memory), Python marks the whole pool broken and
every read waiting on it failed with BrokenProcessPool -- 5 to 7 healthy
systems went "No data" because of one. When a read hung, the pool had to be
killed and replaced, cancelling the other systems' reads ("cancelled twice by
worker restarts").

Now each system has its own long-lived worker process. A crash or a hang
only affects that system's card; the others keep reading. A crash is retried
once on a fresh process, and the error then names the system and the
process's exit code (e.g. 0xC0000005 = access violation in native code), so
the Errors page says which system really kills the worker.

    IBO_RFC_PROCESSES             reads running at the same time [3]
    IBO_RFC_WORKER_IDLE_MINUTES   stop a system's worker unused this long [30]
    IBO_RFC_MAX_WORKERS           at most this many worker processes [20]
"""
from __future__ import annotations

import os
import pickle
import threading
import time

from utils.logger import get_logger

log = get_logger(__name__)


class RfcCallTimeout(RuntimeError):
    """A read did not finish within its time limit; that system's worker was stopped."""


class RfcWorkerDied(RuntimeError):
    """The system's worker process ended during a read (twice: the retry died too)."""


class RfcBusy(RuntimeError):
    """A read of this system is already running; this one was not queued behind it."""


class RfcShuttingDown(RuntimeError):
    """The dashboard is stopping; no new RFC reads are started."""


# ----------------------------------------------------------------- child side
def _safe_exception(exc: BaseException) -> BaseException:
    """The exception itself if the dashboard process can unpickle it, else a
    RuntimeError with the same text (pyrfc's errors take extra constructor
    arguments and do not always survive pickling)."""
    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:  # noqa: BLE001
        return RuntimeError(f"{type(exc).__name__}: {exc}")


def _watch_parent() -> None:
    """End this worker when the dashboard process is gone. A worker stuck in a
    hung RFC call otherwise lived on for 30 min or more after the dashboard
    was killed (seen in the simulation), holding a SAP connection."""
    import multiprocessing as mp
    parent = mp.parent_process()
    if parent is None:
        return
    while True:
        time.sleep(5)
        try:
            alive = parent.is_alive()
        except Exception:  # noqa: BLE001
            alive = False
        if not alive:
            os._exit(0)


def _worker_main(conn) -> None:
    """Loop in the worker: receive (fn, args), send back ("ok", value) or ("err", exc)."""
    threading.Thread(target=_watch_parent, name="parent-watch", daemon=True).start()
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        fn, args = msg
        try:
            reply = ("ok", fn(*args))
        except BaseException as exc:  # noqa: BLE001 -- everything goes back to the caller
            reply = ("err", _safe_exception(exc))
        try:
            conn.send(reply)
        except Exception as exc:  # noqa: BLE001 -- result could not be pickled
            conn.send(("err", RuntimeError(f"result could not be sent back: {type(exc).__name__}: {exc}")))


# ---------------------------------------------------------------- parent side
_KNOWN_EXIT = {
    0xC0000005: "access violation inside native code (NetWeaver RFC SDK / pyrfc)",
    0xC00000FD: "stack overflow",
    0xC0000409: "fail-fast / buffer overrun inside native code",
    0xC0000017: "out of memory",
    0xC000012D: "out of virtual memory (page file full)",
    0xE06D7363: "unhandled C++ exception inside native code",
    0xC000013A: "stopped by Ctrl+C / console closed",
    0x10000: "terminated by another process",
}


def describe_exit(code) -> str:
    if code is None:
        return "exit code unknown"
    if code < 0 and -code < 64:
        return f"killed by signal {-code}"
    u = code & 0xFFFFFFFF
    text = _KNOWN_EXIT.get(u)
    if text:
        return f"exit code 0x{u:08X}: {text}"
    if code == 1:
        return "exit code 1 (killed from outside, e.g. Task Manager, or a Python error at start-up)"
    return f"exit code {code}"


class _Worker:
    def __init__(self, key: str):
        self.key = key
        self.proc = None
        self.conn = None
        self.lock = threading.Lock()        # one read at a time per system
        self.last_used = time.monotonic()
        self.users = 0                      # callers holding this worker (reaper never touches >0)

    @property
    def busy(self) -> bool:
        return self.users > 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def start(self) -> None:
        import multiprocessing as mp
        # "spawn" on every OS: forking a multi-threaded web server can copy a
        # lock some other thread holds (logging, SQLite) and hang.
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_worker_main, args=(child,),
                           name=f"ibops-rfc-{self.key}", daemon=True)
        proc.start()
        child.close()
        self.proc, self.conn = proc, parent

    def stop(self, wait: float = 5.0):
        """Stop the process; returns its exit code (None if unknown)."""
        proc, conn = self.proc, self.conn
        self.proc = self.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is None:
            return None
        try:
            if proc.is_alive():
                proc.terminate()
            proc.join(wait)
        except Exception:  # noqa: BLE001
            pass
        return proc.exitcode

    def _exit_code_after_eof(self):
        """The pipe closed: the process ended or is ending. Its exit code."""
        proc = self.proc
        if proc is not None:
            try:
                proc.join(5)
            except Exception:  # noqa: BLE001
                pass
            code = proc.exitcode
        else:
            code = None
        self.stop(wait=1)
        return code

    def call(self, fn, args, timeout: float):
        """fn(*args) in this worker. Raises fn's exception, RfcCallTimeout, or
        _Died(exit_code) when the process ended during the read."""
        if not self.alive() or self.conn is None:
            self.stop(wait=1)
            self.start()
        try:
            self.conn.send((fn, args))
        except (OSError, EOFError, ValueError):
            raise _Died(self._exit_code_after_eof())
        # poll() also returns True when the worker died (the pipe hits EOF).
        conn = self.conn
        if conn is None:
            raise _Died(None)
        if not conn.poll(timeout):
            self.stop(wait=5)
            raise RfcCallTimeout("timeout")
        try:
            status, value = conn.recv()
        except (EOFError, OSError, AttributeError):
            raise _Died(self._exit_code_after_eof())
        except Exception as exc:  # noqa: BLE001 -- reply could not be unpickled
            raise RuntimeError(f"reply from the RFC worker could not be read: {type(exc).__name__}: {exc}")
        if status == "ok":
            return value
        raise value


class _Died(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


_workers: dict[str, _Worker] = {}
_workers_lock = threading.Lock()
_closed = {"on": False}
_slots = None
_slots_n = None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def max_parallel() -> int:
    return _env_int("IBO_RFC_PROCESSES", 3)


def enabled() -> bool:
    return max_parallel() > 0


def _get_slots() -> threading.Semaphore:
    global _slots, _slots_n
    n = max(1, max_parallel())
    with _workers_lock:
        if _slots is None or _slots_n != n:
            _slots, _slots_n = threading.BoundedSemaphore(n), n
        return _slots


def _reap_locked(now: float) -> list:
    """Workers to stop: idle too long, or the oldest idle ones over the cap."""
    idle_s = max(60, _env_int("IBO_RFC_WORKER_IDLE_MINUTES", 30) * 60)
    # At least one process per system in use: with a cap below the number of
    # systems, workers were stopped and respawned on every pass.
    cap = max(2, _env_int("IBO_RFC_MAX_WORKERS", 64))
    out = []
    for key, w in list(_workers.items()):
        if not w.busy and now - w.last_used > idle_s:
            out.append(_workers.pop(key))
    idle = sorted((w for w in _workers.values() if not w.busy), key=lambda w: w.last_used)
    while len(_workers) > cap and idle:
        w = idle.pop(0)
        out.append(_workers.pop(w.key))
    return out


def _worker_for(key: str) -> _Worker:
    with _workers_lock:
        if _closed["on"]:
            raise RfcShuttingDown("the dashboard is stopping; RFC read not started")
        stale = _reap_locked(time.monotonic())
        w = _workers.get(key)
        if w is None:
            w = _workers[key] = _Worker(key)
        w.users += 1
    for s in stale:
        log.info(f"RFC worker for {s.key} stopped (idle)")
        threading.Thread(target=s.stop, daemon=True).start()
    return w


def run(key: str, fn, args: tuple, timeout: float, wait_if_busy: bool = False):
    """
    fn(*args) in the worker process of `key` (the system name).

    A read of a system that is ALREADY being read is refused at once with
    RfcBusy (the caller keeps the last reading) instead of queueing behind
    it: with one hung system, every refresh pass queued another read of it,
    the duplicates held the read slots and healthy systems went stale while
    threads piled up (simulation: 3 -> 33 threads in 4 min). wait_if_busy=True
    waits for the running read instead.

    The per-system lock is taken BEFORE a read slot, so a waiting read never
    holds a slot. Waiting for a slot does not count toward `timeout`.
    """
    what = f"{getattr(fn, '__name__', fn)} {key}"
    w = _worker_for(key)
    got = False
    try:
        got = w.lock.acquire(blocking=wait_if_busy)
        if not got:
            raise RfcBusy(f"a read of {key} is already running")
        with _get_slots():
            for attempt in (1, 2):
                try:
                    return w.call(fn, args, timeout)
                except RfcCallTimeout:
                    log.warning(f"RFC read {what} exceeded {timeout:.0f}s; only {key}'s worker was stopped")
                    raise RfcCallTimeout(
                        f"RFC read {what} did not finish within {timeout:.0f}s (IBO_RFC_CALL_TIMEOUT); "
                        f"{key}'s worker was restarted. Other systems were not affected.") from None
                except _Died as died:
                    why = describe_exit(died.code)
                    if attempt == 1:
                        log.warning(f"RFC worker for {key} ended during {what} ({why}); "
                                    f"retrying once on a fresh process")
                        continue
                    raise RfcWorkerDied(
                        f"RFC worker for {key} ended during the read, twice ({why}). "
                        f"Only {key} is affected.") from None
    finally:
        if got:
            w.lock.release()
        with _workers_lock:
            w.users -= 1
            w.last_used = time.monotonic()


def status() -> list[dict]:
    """For diagnostics: one row per worker."""
    now = time.monotonic()
    with _workers_lock:
        return [{"system": k, "pid": getattr(w.proc, "pid", None), "alive": w.alive(),
                 "busy": w.busy, "idle_seconds": round(now - w.last_used)}
                for k, w in sorted(_workers.items())]


def shutdown() -> None:
    """Stop every worker; new reads are refused from now on (RfcShuttingDown)."""
    with _workers_lock:
        _closed["on"] = True
        ws = list(_workers.values())
        _workers.clear()
    for w in ws:
        try:
            if w.conn is not None:
                w.conn.send(None)
        except Exception:  # noqa: BLE001
            pass
        w.stop(wait=2)

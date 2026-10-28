"""
ONE lock for every write to config/systems.yaml, the other config YAML files
and .env.

Before, each writer read the file, changed it and wrote it back on its own:
two admins saving within half a second both got "saved", and one change was
silently lost (load test: up to 18 of 30 saves lost, an RFC password left at
its old value in .env). A system save rewrites systems.yaml twice and .env
3-4 times, so the whole save has to be one critical section.

Re-entrant inside a process (a system save calls the .env writer), and an OS
file lock across processes (the collector and the command-line scripts write
too). The OS drops the file lock if a process dies.
"""
from __future__ import annotations

import functools
import os
import threading
import time

_rlock = threading.RLock()
_depth = threading.local()
_file = {"f": None}


def _path() -> str:
    from utils.paths import BASE_DIR
    d = os.environ.get("IBO_RUNTIME_DIR") or os.path.join(str(BASE_DIR), "runtime")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config-write.lock")


def _lock_file(timeout: float) -> None:
    f = open(_path(), "a+b")
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _file["f"] = f
            return
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                raise TimeoutError("another process is saving the configuration; try again in a moment")
            time.sleep(0.05)


def _unlock_file() -> None:
    f, _file["f"] = _file["f"], None
    if f is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()


class _Held:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def __enter__(self):
        if not _rlock.acquire(timeout=self.timeout):
            raise TimeoutError("the configuration is being saved by someone else; try again in a moment")
        depth = getattr(_depth, "n", 0)
        if depth == 0:
            try:
                _lock_file(self.timeout)
            except Exception:
                _rlock.release()
                raise
        _depth.n = depth + 1
        return self

    def __exit__(self, *exc):
        _depth.n -= 1
        if _depth.n == 0:
            _unlock_file()
        _rlock.release()
        return False


def held(timeout: float = 30.0) -> _Held:
    """with config_lock.held(): read-modify-write a config file."""
    return _Held(timeout)


def locked(fn):
    """Decorator: the whole function runs under the config write lock."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        with _Held():
            return fn(*a, **k)
    return wrapper

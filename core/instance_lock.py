"""
Only ONE scheduler on this machine, however many dashboard processes start.

A second dashboard process (an auto-start task firing while one already
runs, or a restart before the old one has exited) used to start its own
scheduler: the same profile then ran twice, or at odd times from the
second process's catch-up. The scheduler now takes an OS file lock first;
a process that cannot get it waits on standby and takes over only if the
holder stops. The OS releases the lock when a process exits or is killed,
so a crash never leaves it stuck.
"""
from __future__ import annotations

import os
import threading

_held: dict[str, object] = {}       # lock file path -> open file
_lock = threading.Lock()


def _path(name: str) -> str:
    from utils.paths import BASE_DIR
    d = os.environ.get("IBO_RUNTIME_DIR") or os.path.join(str(BASE_DIR), "runtime")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.lock")


def acquire(name: str) -> bool:
    """True if this process now holds (or already held) the lock `name`."""
    path = _path(name)
    with _lock:
        if path in _held:
            return True
        f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        try:
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()).encode())
            f.flush()
        except OSError:
            pass
        _held[path] = f
        return True


def release(name: str) -> None:
    with _lock:
        f = _held.pop(_path(name), None)
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


def holder_pid(name: str) -> str:
    try:
        with open(_path(name), "rb") as f:
            return f.read(32).decode(errors="ignore").strip()
    except OSError:
        return ""

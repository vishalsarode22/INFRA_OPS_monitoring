"""
Atomic JSON files with a cross-process lock.

The dashboard and `python main.py` are separate processes, and the
dashboard itself runs several threads. Before this module, a state file
could be written by two of them at once: the second writer truncated the
file while the first was still writing, or a reader saw a half-written
file. With seven systems that was rare. With two collector VMs and 40+
systems it would not be.

Two guarantees:

1. ATOMIC.  Content is written to `<path>.tmp` and renamed over the real
   file. A reader sees either the old file or the new one, never a torn
   one, and a crash mid-write leaves the old file intact.

2. EXCLUSIVE.  A sidecar lock file `<path>.lock` is held with the OS file
   lock (msvcrt on Windows, fcntl elsewhere) for the duration of a write
   or a read-modify-write, so two PROCESSES serialise as well as two
   threads. In-process callers still use their own threading.Lock for the
   cheap case; this is the inter-process layer underneath it.

Every file written here carries "_schema": N so a later migration can tell
what it is reading. Readers ignore the key.

Usage:
    data = read_json(path, default={})
    write_json(path, data)
    with locked(path):           # read-modify-write under one lock
        h = read_json(path, default=[]); h.append(x); write_json(path, h)
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from typing import Any

from utils.logger import get_logger

log = get_logger(__name__)

SCHEMA_KEY = "_schema"
SCHEMA_VERSION = 1
_LOCK_TIMEOUT = 15.0   # seconds; a writer holding a lock longer is stuck

# Re-entrancy: `with locked(p): ... write_json(p)` must not block on itself.
# OS file locks are not re-entrant across file descriptors, so track what
# this thread already holds.
import threading
_held = threading.local()


# OS file locks: msvcrt on Windows, fcntl elsewhere. Defined as plain
# module-level functions (not inside if/else) so run_lock.py's
# `from core.atomic_json import _lock_fd` passes the import-integrity test.
_WINDOWS = sys.platform.startswith("win")
if _WINDOWS:
    import msvcrt
else:
    import fcntl


def _lock_fd(fd: int) -> None:
    """Non-blocking exclusive lock; raises OSError when already held."""
    if _WINDOWS:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    try:
        if _WINDOWS:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def locked(path: str, timeout: float = _LOCK_TIMEOUT):
    """Hold the cross-process lock for `path`. Times out rather than hangs."""
    key = os.path.abspath(path)
    held: set = getattr(_held, "paths", None)
    if held is None:
        held = _held.paths = set()
    if key in held:
        yield            # already locked by this thread: re-entrant
        return
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    held.add(key)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    # Proceed anyway: a stuck lock must not stop monitoring,
                    # but say so, because it means a writer died mid-write.
                    log.warning(f"lock on {os.path.basename(path)} not acquired in "
                                f"{timeout:.0f}s; proceeding without it")
                    break
                time.sleep(0.05)
        yield
    finally:
        held.discard(key)
        _unlock_fd(fd)
        os.close(fd)


def _sqlite_backend() -> bool:
    """IBO_STATE_BACKEND=sqlite (default) keeps state in core/store.py's database."""
    return (os.getenv("IBO_STATE_BACKEND", "sqlite") or "sqlite").strip().lower() != "files"


def read_json(path: str, default: Any = None) -> Any:
    """Read; return `default` on missing or unreadable file (logged)."""
    if _sqlite_backend():
        from core import store
        data = store.get_document(path, default)
        if isinstance(data, dict):
            data.pop(SCHEMA_KEY, None)
        return data
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning(f"{os.path.basename(path)}: unreadable ({type(exc).__name__}); "
                    f"using default")
        return default
    if isinstance(data, dict):
        data.pop(SCHEMA_KEY, None)
    return data


def write_json(path: str, data: Any, indent: int = 2) -> None:
    """Write atomically under the cross-process lock (or into the store)."""
    if _sqlite_backend():
        from core import store
        store.put_document(path, data)
        return
    payload = data
    if isinstance(data, dict):
        payload = dict(data)
        payload[SCHEMA_KEY] = SCHEMA_VERSION
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with locked(path):
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=indent, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        from utils.atomic import replace_or_overwrite   # Windows: file in use by readers
        replace_or_overwrite(tmp, path)


def read_lines(path: str):
    """Iterate the lines of a JSONL file (or its rows in the store)."""
    if _sqlite_backend():
        from core import store
        yield from store.read_lines(path)
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                yield line.rstrip("\n")
    except FileNotFoundError:
        return


def list_json(prefix: str) -> list[str]:
    """Relative paths of the JSON documents under a directory."""
    if _sqlite_backend():
        from core import store
        return store.list_documents(prefix)
    if not os.path.isdir(prefix):
        return []
    return sorted(os.path.join(prefix, f).replace("\\", "/") for f in os.listdir(prefix)
                  if f.endswith(".json"))


def append_line(path: str, line: str) -> None:
    """Append one line (JSONL) under the lock; atomic enough for a log."""
    if _sqlite_backend():
        from core import store
        store.append_lines(path, [line])
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with locked(path):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")

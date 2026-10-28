"""
One sweep at a time, across processes.

The dashboard's scheduler thread and `python main.py` (Task Scheduler,
run_pipeline.bat) can both start a sweep. A sweep drives SAP GUI; two at
once fight over one desktop and write the same snapshot. The in-process
_run_state in dashboard/app.py cannot see the other process, so this lock
file does: dashboard/sweep.lock holds the owner's PID, host and start time
and is held with the OS file lock for the whole sweep.

    with sweep_lock("PS4") as acquired:
        if not acquired:
            ...someone else is sweeping; skip, say who...

A dead owner (crashed process) leaves no lock behind: OS file locks are
released when the process exits, so no stale-lock cleanup is needed.
`owner()` reads the sidecar to say who holds it, for log and UI.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from datetime import datetime

from core.atomic_json import _lock_fd, _unlock_fd
from utils.logger import get_logger

log = get_logger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(_BASE, "dashboard", "sweep.lock")


def owner() -> dict | None:
    """Who holds the sweep lock right now, or None."""
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as fh:
            info = json.load(fh)
    except Exception:
        return None
    # The file persists after release; the OS lock does not. Probe it.
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            _lock_fd(fd)
            _unlock_fd(fd)
            return None            # we got it, so nobody holds it
        except OSError:
            return info
    finally:
        os.close(fd)


@contextlib.contextmanager
def sweep_lock(system: str, wait_seconds: float = 0):
    """Yield True when this process holds the sweep lock, else False."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    acquired = False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                _lock_fd(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(1)
        if acquired:
            info = {"pid": os.getpid(), "host": socket.gethostname(), "system": system,
                    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, json.dumps(info).encode("utf-8"))
            except OSError:
                pass
        else:
            who = owner() or {}
            log.warning(f"{system}: sweep NOT started -- another sweep holds the lock "
                        f"(pid {who.get('pid')}, {who.get('system')}, since {who.get('started')}).")
        yield acquired
    finally:
        if acquired:
            _unlock_fd(fd)
        os.close(fd)

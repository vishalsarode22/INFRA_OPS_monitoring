"""
Pipeline progress heartbeat.

WHY THIS EXISTS
---------------
The watchdog in main.py used a single wall-clock deadline: "if the pipeline
thread has not RETURNED within N seconds, it is hung". That conflates two very
different states.

    slow  -- a PRD sweep with 22 T-codes, a 118s STAT read and an AI call
             legitimately takes 6-10 minutes and is making progress the whole
             time.
    hung  -- a COM call blocked on a modal SAP dialog, making no progress at
             all, and no amount of waiting will help.

With a fixed deadline the only way to stop killing the first case is to raise
the timeout so high that the second case is not caught for a quarter of an
hour.

A heartbeat separates them. Any long-running stage touches beat(); the watchdog
asks "how long since the last sign of life?" instead of "how long in total?".
A run that is progressing is never killed no matter how long it takes, and a
genuine hang is caught in STALL_SECONDS regardless of how far into the run it
happened.

Deliberately dependency-free and module-level: it is imported from the SAP GUI
layer, the collectors and main, and must never be the thing that fails.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_state: dict[str, float | str | None] = {"at": time.monotonic(), "stage": None}


def beat(stage: str | None = None) -> None:
    """Record a sign of life. Safe to call from any thread, cheap enough to
    call per T-code."""
    with _lock:
        _state["at"] = time.monotonic()
        if stage:
            _state["stage"] = stage


def reset(stage: str | None = "start") -> None:
    """Start a fresh run. Call immediately before launching the pipeline
    thread, so the previous run's last beat cannot count toward this one."""
    beat(stage)


def seconds_since_beat() -> float:
    with _lock:
        return time.monotonic() - float(_state["at"])


def last_stage() -> str | None:
    """The stage name from the most recent beat -- used in the watchdog's log
    line so a stall report names WHERE it stalled, not just that it did."""
    with _lock:
        return _state["stage"]

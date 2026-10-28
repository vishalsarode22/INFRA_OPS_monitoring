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

PER-RUN HEARTBEATS
------------------
There used to be ONE module-level heartbeat. When the watchdog gave up on a
hung pipeline thread and the next run started, the abandoned thread kept
beating into the same record (so a hung new run could look alive) and later
called kill_sap_processes()/close on the NEXT run's SAP GUI.

Each pipeline run now has its own Run object, bound to the pipeline thread
(and to the threads it starts with contextvars.copy_context()) through a
context variable. beat() from that context updates THAT run only; the
watchdog reads the run it started, never a shared value.

A run the watchdog gives up on is CANCELLED (and, when its thread does not
unwind, ABANDONED). A cancelled run's beats are ignored, and the pipeline
checks cancelled() at stage boundaries and before any SAP kill/close or
delivery, so a zombie thread cannot act after abandonment.

Deliberately dependency-free: it is imported from the SAP GUI layer, the
collectors and main, and must never be the thing that fails.
"""

from __future__ import annotations

import contextvars
import itertools
import threading
import time

_lock = threading.Lock()
_ids = itertools.count(1)


class Run:
    """Heartbeat, progress and cancel flag of one pipeline run."""

    def __init__(self, run_id: str | None = None, stage: str | None = "start"):
        self.run_id = run_id or f"run-{int(time.time())}-{next(_ids)}"
        self._lock = threading.Lock()
        self.at = time.monotonic()
        self.stage = stage
        self.progress = {"tcode": None, "index": 0, "total": 0}
        self.cancelled_reason: str | None = None
        self.abandoned = False

    # -- liveness ---------------------------------------------------------
    def beat(self, stage: str | None = None) -> None:
        with self._lock:
            if self.cancelled_reason is not None:
                return          # a cancelled run never looks alive again
            self.at = time.monotonic()
            if stage:
                self.stage = stage

    def set_progress(self, tcode=None, index: int = 0, total: int = 0) -> None:
        with self._lock:
            if self.cancelled_reason is not None:
                return
            self.progress = {"tcode": tcode, "index": int(index or 0), "total": int(total or 0)}
            self.at = time.monotonic()

    def seconds_since_beat(self) -> float:
        with self._lock:
            return time.monotonic() - self.at

    # -- cancellation -----------------------------------------------------
    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self.cancelled_reason is None:
                self.cancelled_reason = reason or "cancelled"

    def abandon(self, reason: str = "abandoned") -> None:
        self.cancel(reason)
        with self._lock:
            self.abandoned = True

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self.cancelled_reason is not None

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.progress, "stage": self.stage, "run_id": self.run_id,
                    "idle_s": round(time.monotonic() - self.at),
                    "cancelled": self.cancelled_reason}


# The run the current thread/context works for (bound by the watchdog inside
# the pipeline thread, inherited by copy_context() worker threads).
_context_run: contextvars.ContextVar = contextvars.ContextVar("ibo_run", default=None)

# The run the dashboard shows (the most recently started one).
_current: Run = Run(stage=None)


def start_run(run_id: str | None = None, stage: str | None = "start") -> Run:
    """A fresh Run that becomes the one the UI shows. It is NOT bound to the
    calling context -- call bind() inside the pipeline thread."""
    global _current
    run = Run(run_id, stage)
    with _lock:
        _current = run
    return run


def bind(run: Run | None):
    """Bind `run` to the calling context. Returns a token for unbind()."""
    return _context_run.set(run)


def unbind(token) -> None:
    try:
        _context_run.reset(token)
    except (ValueError, RuntimeError):
        pass


def context_run() -> Run | None:
    return _context_run.get()


def current_run() -> Run:
    """The run of the calling context, else the run the UI shows."""
    run = _context_run.get()
    if run is not None:
        return run
    with _lock:
        return _current


def cancelled() -> bool:
    """True when the calling context belongs to a run the watchdog cancelled
    or abandoned. Code outside any run (dashboard, tests) is never cancelled."""
    run = _context_run.get()
    return bool(run is not None and run.cancelled)


def cancel_reason() -> str | None:
    run = _context_run.get()
    return run.cancelled_reason if run is not None else None


# ---------------------------------------------------------------------------
# Module-level API kept for every existing caller (collectors, SAP GUI layer,
# the wall). It acts on the calling context's run, falling back to the run
# the UI shows when called from a thread that does not carry the context.
# ---------------------------------------------------------------------------

def beat(stage: str | None = None) -> None:
    """Record a sign of life. Safe to call from any thread, cheap enough to
    call per T-code."""
    current_run().beat(stage)


def reset(stage: str | None = "start") -> None:
    """Start a fresh run (legacy entry point): a new Run becomes current, so
    the previous run's last beat cannot count toward this one."""
    start_run(stage=stage)


def progress(tcode: str | None = None, index: int = 0, total: int = 0) -> None:
    """Which T-code the sweep is on (n of m) -- shown on the wall while running."""
    current_run().set_progress(tcode, index, total)


def progress_snapshot() -> dict:
    with _lock:
        run = _current
    snap = run.snapshot()
    return {"tcode": snap["tcode"], "index": snap["index"], "total": snap["total"],
            "stage": snap["stage"], "idle_s": snap["idle_s"]}


def seconds_since_beat() -> float:
    return current_run().seconds_since_beat()


def last_stage() -> str | None:
    """The stage name from the most recent beat -- used in the watchdog's log
    line so a stall report names WHERE it stalled, not just that it did."""
    return current_run().stage

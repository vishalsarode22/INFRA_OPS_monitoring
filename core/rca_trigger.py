"""
Performance RCA trigger.

Decides WHEN an emergency RCA sweep runs. It does not run it -- that is
evaluation.rca_pipeline -- and it does not read SAP; it is fed the same live
payload the wall already has, once per poll, and answers yes or no.

Three rules, in order, and every one exists because of a specific way this
would otherwise misfire:

  sustain    one poll over threshold is a blip (a single 4s dialog step in a
             30s window will do it). Two consecutive is a spike. Firing on one
             would run a multi-minute GUI sweep for something that was over
             before the sweep began.

  cooldown   a spike lasts longer than a sweep. Without a cooldown the same
             spike fires an RCA every poll for as long as it lasts, and the
             consultant gets five emails about one incident.

  pre-empt   the operator chose "stop the sweep, run RCA now". Pre-emption
             is COOPERATIVE: the running sweep is asked to stop and finishes
             the system it is on. It is not killed, because a sweep aborted
             mid-system leaves SAP Logon open and a half-written snapshot --
             a worse state than an RCA that starts ninety seconds late.

State is per-system and lives in memory. A restart forgets it, which is the
right default: after a restart nothing is known about the last spike, so
the first breach counts from zero again.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

from utils.logger import get_logger

log = get_logger(__name__, "application")


# The cooldown must hold across processes: the dashboard polls in one process
# and a sweep runs in another, and both can decide to fire.
RCA_STATE_PATH = Path(__file__).resolve().parents[1] / "logs" / "rca_last_fired.json"


def _read_state() -> dict:
    try:
        with open(RCA_STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def last_fired_at(system: str):
    """When an RCA last ran for this system, from any process."""
    stamp = str((_read_state().get(system) or {}).get("at") or "")
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def mark_fired_on_disk(system: str, reason: str) -> None:
    state = _read_state()
    state[system] = {"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "reason": reason}
    try:
        RCA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RCA_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
    except OSError as exc:
        log.warning(f"RCA state not written ({RCA_STATE_PATH}): {exc}")


def sweep_decision(system: str, value_ms, instance: str, cfg: "TriggerConfig", now=None) -> tuple:
    """
    Whether the sweep's own SMLG reading should start an RCA.

    The live-poll trigger watches ST03's short-window figure. On PS4,
    23.09.2026, SMLG read 2,404 ms while the live figure was about 120 ms,
    so nothing fired and the RCA had to be started by hand -- six minutes
    later, when the spike was over.
    """
    if not cfg.enabled:
        return False, "RCA disabled"
    try:
        value = float(value_ms)
    except (TypeError, ValueError):
        return False, "no SMLG reading"
    if value < cfg.threshold_ms:
        return False, f"{value:.0f} ms below the {cfg.threshold_ms:.0f} ms threshold"
    last = last_fired_at(system)
    now = now or datetime.now()
    if last and now - last < timedelta(minutes=cfg.cooldown_minutes):
        left = cfg.cooldown_minutes - (now - last).total_seconds() / 60
        return False, f"{value:.0f} ms over threshold but in cooldown ({left:.0f} min left)"
    where = f" on {instance}" if instance else ""
    return True, f"sweep: SMLG {value:.0f} ms{where} over {cfg.threshold_ms:.0f} ms"


@dataclass
class TriggerConfig:
    metric: str = "sap.st03.dialog_resp_ms"
    threshold_ms: float = 1500.0
    sustain_polls: int = 2
    cooldown_minutes: float = 30.0
    preempt_running_sweep: bool = True
    enabled: bool = True

    @classmethod
    def from_yaml(cls, cfg: dict) -> "TriggerConfig":
        t = (cfg or {}).get("trigger") or {}
        return cls(
            metric=str(t.get("metric", cls.metric)),
            threshold_ms=float(t.get("threshold_ms", cls.threshold_ms)),
            sustain_polls=max(1, int(t.get("sustain_polls", cls.sustain_polls))),
            cooldown_minutes=float(t.get("cooldown_minutes", cls.cooldown_minutes)),
            preempt_running_sweep=bool(t.get("preempt_running_sweep", cls.preempt_running_sweep)),
            enabled=bool((cfg or {}).get("enabled", True)),
        )


@dataclass
class SystemState:
    consecutive_over: int = 0
    last_value: Optional[float] = None
    last_fired_at: Optional[float] = None       # monotonic
    last_reason: str = ""
    history: list = field(default_factory=list)  # (monotonic, value) recent readings


@dataclass
class Decision:
    fire: bool
    reason: str
    value: Optional[float] = None
    consecutive: int = 0
    cooldown_remaining_s: float = 0.0


def response_ms_from_payload(payload: dict, metric: str) -> Optional[float]:
    """
    The dialog response figure the wall shows for a system.

    Prefer the aggregated median the wall's card uses; fall back to the worst
    instance, because a single saturated app server is exactly the case an
    RCA is for and a fleet median can hide it.
    """
    if not isinstance(payload, dict) or not payload.get("connected", True):
        return None
    v = payload.get("dialog_response_ms")
    if isinstance(v, (int, float)):
        return float(v)
    for m in payload.get("rfc_metrics") or []:
        if m.get("metric") == metric and isinstance(m.get("value"), (int, float)):
            return float(m["value"])
    per_inst = payload.get("perf_response_by_instance") or {}
    vals = [float(x) for x in per_inst.values() if isinstance(x, (int, float))]
    if vals:
        return max(vals)
    return None


class RcaTrigger:
    def __init__(self, config: TriggerConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = config
        self._clock = clock
        self._states: dict[str, SystemState] = {}
        self._lock = threading.Lock()

    def state(self, system: str) -> SystemState:
        return self._states.setdefault(system, SystemState())

    def observe(self, system: str, payload: dict) -> Decision:
        """Feed one live poll. Returns whether an RCA should fire now."""
        if not self.cfg.enabled:
            return Decision(False, "disabled")
        value = response_ms_from_payload(payload, self.cfg.metric)
        now = self._clock()
        with self._lock:
            st = self.state(system)
            st.last_value = value
            st.history.append((now, value))
            del st.history[:-20]

            if value is None:
                # No reading is not a breach and not a recovery; leave the
                # streak alone so a flaky poll does not reset a real spike.
                return Decision(False, "no reading", None, st.consecutive_over)

            if value < self.cfg.threshold_ms:
                st.consecutive_over = 0
                return Decision(False, "under threshold", value, 0)

            st.consecutive_over += 1
            if st.consecutive_over < self.cfg.sustain_polls:
                return Decision(False, f"over threshold ({st.consecutive_over}/{self.cfg.sustain_polls})",
                                value, st.consecutive_over)

            remaining = self._cooldown_remaining(st, now)
            if remaining > 0:
                return Decision(False, "in cooldown", value, st.consecutive_over, remaining)

            st.last_fired_at = now
            st.last_reason = (f"{self.cfg.metric} = {value:.0f} ms over {self.cfg.threshold_ms:.0f} ms "
                              f"for {st.consecutive_over} consecutive polls")
            st.consecutive_over = 0   # a fresh spike after cooldown must re-sustain
            return Decision(True, st.last_reason, value, self.cfg.sustain_polls)

    def mark_fired(self, system: str, reason: str = "manual") -> None:
        """Record a manual run so the auto-trigger honours the same cooldown."""
        with self._lock:
            st = self.state(system)
            st.last_fired_at = self._clock()
            st.last_reason = reason
            st.consecutive_over = 0

    def _cooldown_remaining(self, st: SystemState, now: float) -> float:
        if st.last_fired_at is None:
            return 0.0
        return max(0.0, self.cfg.cooldown_minutes * 60.0 - (now - st.last_fired_at))

    def status(self, system: str) -> dict:
        with self._lock:
            st = self.state(system)
            now = self._clock()
            return {
                "system": system,
                "enabled": self.cfg.enabled,
                "metric": self.cfg.metric,
                "threshold_ms": self.cfg.threshold_ms,
                "last_value_ms": st.last_value,
                "consecutive_over": st.consecutive_over,
                "sustain_polls": self.cfg.sustain_polls,
                "cooldown_remaining_s": round(self._cooldown_remaining(st, now)),
                "last_reason": st.last_reason,
            }


# --------------------------------------------------------------------------
# Pre-emption
# --------------------------------------------------------------------------

def preempt_running_sweep(run_state: dict, request_cancel: Callable[[], None],
                          is_running: Callable[[], bool], wait_s: float = 600.0,
                          poll_s: float = 1.0, clock: Callable[[], float] = time.monotonic,
                          sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str]:
    """
    Ask a running sweep to stop and wait for it to actually stop.

    Returns (ok, note). ok is False only if the sweep is still running after
    wait_s -- at which point the caller must NOT touch SAP GUI, because two
    drivers on one session is the thing the heartbeat work exists to prevent.

    wait_s defaults to ten minutes because that is the upper end of a normal
    single-system sweep; a sweep still running past that is one the watchdog
    should already be killing.
    """
    if not is_running():
        return True, "no sweep was running"
    current = run_state.get("current_system")
    log.warning(f"RCA pre-empting the running sweep ({current}): requesting stop, "
                f"will wait up to {wait_s:.0f}s for it to finish the current system")
    request_cancel()
    deadline = clock() + wait_s
    while clock() < deadline:
        if not is_running():
            return True, f"pre-empted sweep on {current}"
        sleep(poll_s)
    return False, (f"sweep on {current} still running after {wait_s:.0f}s; "
                   f"RCA not started (would collide on SAP GUI)")

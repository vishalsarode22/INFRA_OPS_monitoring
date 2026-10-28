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
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

from utils.logger import get_logger, LOG_DIR

log = get_logger(__name__, "application")


# The cooldown must hold across processes: the dashboard polls in one process
# and a sweep runs in another, and both can decide to fire.
RCA_STATE_PATH = Path(LOG_DIR) / "rca_last_fired.json"


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


def mark_fired_on_disk(system: str, reason: str, now: datetime | None = None,
                       base_minutes: float | None = None) -> None:
    """
    Record an RCA run: when, why, how many today, and the back-off streak.

    Each RCA spends AI quota (36 runs in one day were seen). A run that
    follows the previous one within twice its cooldown extends the streak,
    and the next cooldown doubles: 30 -> 60 -> 120 min (rca_cooldown_minutes).
    """
    now = now or datetime.now()
    state = _read_state()
    prev = state.get(system) or {}
    base = float(base_minutes or prev.get("base") or 30.0)
    day = now.strftime("%Y-%m-%d")
    count = int(prev.get("count") or 0) + 1 if prev.get("day") == day else 1
    streak = 1
    last = last_fired_at(system)
    if last is not None:
        prev_cool = rca_cooldown_minutes(system, base)
        if (now - last).total_seconds() <= 2 * prev_cool * 60:
            streak = int(prev.get("streak") or 1) + 1
    state[system] = {"at": now.strftime("%Y-%m-%dT%H:%M:%S"), "reason": reason,
                     "day": day, "count": count, "streak": streak, "base": base}
    try:
        RCA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RCA_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
    except OSError as exc:
        log.warning(f"RCA state not written ({RCA_STATE_PATH}): {exc}")


def daily_cap(system=None) -> int:
    """Automatic RCA runs per system per day: the system's alert_timing.rca_daily_cap,
    else IBO_RCA_DAILY_CAP (default 4); 0 = no cap."""
    if system:
        from core import alert_timing
        own = alert_timing.system_value(system, "rca_daily_cap")
        if own is not None:
            return max(0, own)
    try:
        return max(0, int(os.getenv("IBO_RCA_DAILY_CAP", "4") or 4))
    except ValueError:
        return 4


def rca_cooldown_minutes(system: str, base_minutes: float) -> float:
    """The cooldown now in force: base x 2^(streak-1), at most 4 x base (30/60/120)."""
    rec = _read_state().get(system) or {}
    streak = max(1, int(rec.get("streak") or 1))
    return float(base_minutes) * min(4, 2 ** (streak - 1))


def rca_budget_block(system: str, base_minutes: float, now: datetime | None = None) -> str | None:
    """Why an AUTOMATIC RCA must not run now (back-off cooldown, daily cap), or None."""
    now = now or datetime.now()
    rec = _read_state().get(system) or {}
    cap = daily_cap(system)
    if cap and rec.get("day") == now.strftime("%Y-%m-%d") and int(rec.get("count") or 0) >= cap:
        return f"daily cap of {cap} RCA runs reached (Systems page timing / IBO_RCA_DAILY_CAP)"
    last = last_fired_at(system)
    if last is not None:
        cool = rca_cooldown_minutes(system, base_minutes)
        left = cool - (now - last).total_seconds() / 60
        if left > 0:
            return f"in cooldown ({left:.0f} min left of {cool:.0f})"
    return None


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
    if not getattr(cfg, "automatic", False):
        return False, "automatic RCA is off (Performance monitoring button only)"
    try:
        value = float(value_ms)
    except (TypeError, ValueError):
        return False, "no SMLG reading"
    limit = critical_limit_ms(system, cfg) if cfg.mode == "instance" else cfg.threshold_ms
    if value < limit:
        return False, f"{value:.0f} ms below the {limit:.0f} ms threshold"
    now = now or datetime.now()
    block = rca_budget_block(system, cfg.cooldown_minutes, now)
    if block:
        return False, f"{value:.0f} ms over threshold but {block}"
    where = f" on {instance}" if instance else ""
    return True, f"sweep: SMLG {value:.0f} ms{where} at or over {limit:.0f} ms"


def _auto_on(trigger: dict) -> bool:
    import os
    env = (os.getenv("IBO_RCA_AUTO") or "").strip().lower()
    if env:
        return env in ("1", "true", "yes", "on")
    return bool((trigger or {}).get("automatic", False))


@dataclass
class TriggerConfig:
    # mode "instance": any instance's 5-min STAT AVERAGE at or over the
    # system's CRITICAL limit for sap.smlg.response_time (Thresholds page,
    # per system; default 2000 ms) fires -- once per system, naming the slow
    # instance(s). mode "system": the older rule on one system-wide figure
    # (`metric` over `threshold_ms` for `sustain_polls` polls). The defaults
    # below are the older rule; config/rca.yaml switches on "instance".
    mode: str = "system"
    metric: str = "sap.st03.dialog_resp_ms"
    threshold_ms: float = 1500.0
    sustain_polls: int = 2
    cooldown_minutes: float = 30.0
    preempt_running_sweep: bool = True
    enabled: bool = True
    # Owner's decision (27.10.2026): no automatic RCA -- only the Performance
    # monitoring button. from_yaml (what the dashboard uses) reads
    # trigger.automatic from rca.yaml, default OFF; IBO_RCA_AUTO=1 in .env
    # switches it back on. A config built by hand keeps the trigger logic on.
    automatic: bool = True

    @classmethod
    def from_yaml(cls, cfg: dict) -> "TriggerConfig":
        t = (cfg or {}).get("trigger") or {}
        return cls(
            mode=str(t.get("mode", cls.mode)).strip().lower(),
            metric=str(t.get("metric", cls.metric)),
            threshold_ms=float(t.get("threshold_ms", cls.threshold_ms)),
            sustain_polls=max(1, int(t.get("sustain_polls", cls.sustain_polls))),
            cooldown_minutes=float(t.get("cooldown_minutes", cls.cooldown_minutes)),
            preempt_running_sweep=bool(t.get("preempt_running_sweep", cls.preempt_running_sweep)),
            enabled=bool((cfg or {}).get("enabled", True)),
            automatic=_auto_on(t),
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


def critical_limit_ms(system: str, cfg: "TriggerConfig") -> float:
    """This system's CRITICAL limit for sap.smlg.response_time (Thresholds page), else the config."""
    try:
        from core.grading import limits_for
        lim = limits_for("sap.smlg.response_time", system)
        if lim["enabled"] and lim["critical"] is not None:
            return float(lim["critical"])
        if not lim["enabled"]:
            return float("inf")              # switched off for this system: never fires
    except Exception:  # noqa: BLE001
        pass
    return float(cfg.threshold_ms)


def slow_instances(system: str, payload: dict, cfg: "TriggerConfig") -> tuple:
    """
    ([(instance, avg_ms), ...] at or over the critical limit, limit, worst avg).

    Only the 5-minute STAT AVERAGE counts (response_statistic "average" from a
    STAT window), and only instances with enough dialog steps to grade
    (response_low_confidence not set). SMLG's rolling figure, a daily
    aggregate or an idle instance never fires an RCA.
    """
    limit = critical_limit_ms(system, cfg)
    if not isinstance(payload, dict) or not payload.get("connected", True):
        return [], limit, None
    slow, worst = [], None
    for inst in payload.get("instances") or []:
        v = inst.get("response_ms")
        src = str(inst.get("response_source") or "")
        if not isinstance(v, (int, float)) or not src.startswith("STAT") \
                or inst.get("response_statistic") != "average" or not src.endswith("min"):
            continue
        if inst.get("response_low_confidence"):
            continue
        worst = v if worst is None else max(worst, v)
        if v >= limit:
            slow.append((str(inst.get("name") or inst.get("host") or "?"), float(v)))
    slow.sort(key=lambda x: -x[1])
    return slow, limit, worst


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
        if not getattr(self.cfg, "automatic", False):
            return Decision(False, "automatic RCA is off")
        if self.cfg.mode == "instance":
            return self._observe_instances(system, payload)
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
            block = rca_budget_block(system, self.cfg.cooldown_minutes)
            if block:
                return Decision(False, block, value, st.consecutive_over)

            st.last_fired_at = now
            st.last_reason = (f"{self.cfg.metric} = {value:.0f} ms over {self.cfg.threshold_ms:.0f} ms "
                              f"for {st.consecutive_over} consecutive polls")
            st.consecutive_over = 0   # a fresh spike after cooldown must re-sustain
            return Decision(True, st.last_reason, value, self.cfg.sustain_polls)

    def _observe_instances(self, system: str, payload: dict) -> Decision:
        slow, limit, worst = slow_instances(system, payload, self.cfg)
        now = self._clock()
        with self._lock:
            st = self.state(system)
            st.last_value = worst
            st.history.append((now, worst))
            del st.history[:-20]
            if not slow:
                st.consecutive_over = 0
                return Decision(False, "no instance at the critical limit" if worst is not None
                                else "no reading", worst, 0)
            st.consecutive_over += 1
            if st.consecutive_over < self.cfg.sustain_polls:
                return Decision(False, f"over limit ({st.consecutive_over}/{self.cfg.sustain_polls})",
                                worst, st.consecutive_over)
            remaining = self._cooldown_remaining(st, now)
            if remaining > 0:
                return Decision(False, "in cooldown", worst, st.consecutive_over, remaining)
            # An RCA from the sweep path counts too; back-off and daily cap on disk.
            block = rca_budget_block(system, self.cfg.cooldown_minutes)
            if block:
                return Decision(False, block, worst, st.consecutive_over)
            names = ", ".join(f"{n} {v:.0f} ms" for n, v in slow)
            st.last_fired_at = now
            st.last_reason = (f"instance response (5-min STAT average) at or over the critical "
                              f"limit {limit:.0f} ms: {names}")
            st.consecutive_over = 0
            return Decision(True, st.last_reason, worst, self.cfg.sustain_polls)

    def cancel_fired(self, system: str) -> None:
        """The RCA this trigger asked for did not start: forget that it fired."""
        with self._lock:
            st = self.state(system)
            st.last_fired_at = None
            st.last_reason = ""

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
                "mode": self.cfg.mode,
                "threshold_ms": (critical_limit_ms(system, self.cfg) if self.cfg.mode == "instance"
                                 else self.cfg.threshold_ms),
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

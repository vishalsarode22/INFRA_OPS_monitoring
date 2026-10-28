"""
Two-tier alert escalation.

    first breach of a metric            -> YELLOW: Basis consultants only
    same metric still breached on the
    next sweep (or any later repeat)    -> RED:    consultants + Basis manager

The count is per (system, metric) and persists across restarts in
data/escalation_state.json, so a restart between two sweeps does not
silently reset a red alert back to yellow. A metric that comes back below
its threshold resets to zero; the next breach starts again at yellow.

Configuration (.env, or per system in systems.yaml):
    ALERT_MANAGER_EMAILS=mgr@company.com,head@company.com   # tier-2 list
    ESCALATION_REPEAT_THRESHOLD=2      # breaches in a row that turn it red
    manager_receivers: ${SYS_MANAGER_RECEIVERS}              # per-system key

WhatsApp and any other channel can ask the same question through
escalation.assess(); the e-mail path is the first caller.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List

from utils.atomic import atomic_write_json, read_json

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATE_PATH = os.path.join(_ROOT, "data", "escalation_state.json")
_lock = threading.Lock()


@dataclass
class Assessment:
    level: str                          # "yellow" | "red"
    repeated: List[str] = field(default_factory=list)   # metrics at/over threshold
    first_time: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    @property
    def escalate(self) -> bool:
        return self.level == "red"


def repeat_threshold() -> int:
    try:
        return max(2, int(os.getenv("ESCALATION_REPEAT_THRESHOLD", "2")))
    except ValueError:
        return 2


def manager_emails(system_cfg: dict | None = None) -> List[str]:
    """Tier-2 recipients: per-system `manager_receivers`, else ALERT_MANAGER_EMAILS."""
    raw = (system_cfg or {}).get("manager_receivers") or os.getenv("ALERT_MANAGER_EMAILS", "")
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [e.strip() for e in str(raw).split(",") if e.strip()]


def assess(system: str, critical_metrics: Iterable[str], *,
           state_path: str | None = None, now: datetime | None = None) -> Assessment:
    """
    Record this sweep's critical metrics for `system` and say which tier the
    alert belongs to. Call once per sweep, after the thresholds have been
    evaluated.
    """
    critical = sorted({str(m) for m in critical_metrics if m})
    threshold = repeat_threshold()
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    state_path = state_path or STATE_PATH

    with _lock:
        state = read_json(state_path, {}) or {}
        sys_state: Dict[str, dict] = state.get(system, {})

        counts: Dict[str, int] = {}
        for metric in critical:
            prev = int((sys_state.get(metric) or {}).get("count", 0))
            counts[metric] = prev + 1
            sys_state[metric] = {"count": prev + 1,
                                 "first_seen": (sys_state.get(metric) or {}).get("first_seen", stamp),
                                 "last_seen": stamp}
        # Anything not critical this sweep has recovered.
        for metric in list(sys_state):
            if metric not in counts:
                del sys_state[metric]

        state[system] = sys_state
        try:
            atomic_write_json(state_path, state)
        except OSError:
            pass    # escalation still works for this sweep; only persistence failed

    repeated = [m for m, c in counts.items() if c >= threshold]
    first = [m for m, c in counts.items() if c < threshold]
    return Assessment("red" if repeated else "yellow", repeated, first, counts)


def clear(system: str | None = None, *, state_path: str | None = None) -> None:
    """Forget breach history -- e.g. after a system is deleted."""
    state_path = state_path or STATE_PATH
    with _lock:
        state = read_json(state_path, {}) or {}
        if system is None:
            state = {}
        else:
            state.pop(system, None)
        atomic_write_json(state_path, state)

"""Runtime coordinator for deterministic operational intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Iterable

from core.baseline_integration import BaselineEngine, MetricIntelligence
from core.change_detector import ChangeSignal, MetricSample, detect_changes
from core.incident_store import load_incidents
from core.intelligence_fusion import OperationalIntelligence, fuse_intelligence
from core.recurrence import IncidentOccurrence, assess_recurrence
from core.models import MonitoringResult
from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "intelligence")


@dataclass
class IntelligenceRuntime:
    """Bounded in-memory state used across monitoring cycles."""

    max_samples: int = 240
    min_baseline_samples: int = 5
    change_lookback_seconds: int = 900
    change_min_samples: int = 3
    recurrence_window_days: int = 30
    recurrence_min_occurrences: int = 3
    state_dir: str | None = None

    def __post_init__(self):
        self._effective_min_samples = min(self.min_baseline_samples, self.max_samples)
        self.baseline = BaselineEngine(
            max_samples=self.max_samples,
            min_samples=self._effective_min_samples,
        )
        self._samples: dict[str, list[MetricSample]] = {}
        self.state_dir = Path(
            self.state_dir or os.path.join(BASE_DIR, "dashboard", "intelligence_state")
        )
        self._load_state()

    @staticmethod
    def _safe_system_name(system: str) -> str:
        safe = "".join(c for c in str(system) if c.isalnum() or c in "-_")
        return safe or "unknown"

    def _state_path(self, system: str) -> Path:
        return self.state_dir / f"{self._safe_system_name(system)}.json"

    def _load_state(self) -> None:
        """Restore bounded samples from disk; corrupt state is ignored safely."""
        # The runtime is created before a system is known, so the registry
        # passes the system into persist/restore explicitly. This method is
        # intentionally a no-op for direct construction without a system.
        return

    def load_system_state(self, system: str) -> None:
        """Load persisted metric samples for one SAP system."""
        path = self._state_path(system)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            samples = payload.get("samples", {})
            self._samples.clear()
            self.baseline = BaselineEngine(
                max_samples=self.max_samples,
                min_samples=self._effective_min_samples,
            )
            for metric, raw_values in samples.items():
                restored = []
                for item in raw_values[-self.max_samples:]:
                    timestamp = datetime.fromisoformat(item["timestamp"])
                    value = float(item["value"])
                    sample = MetricSample(timestamp, metric, value)
                    restored.append(sample)
                    self.baseline.add_sample(metric, value)
                if restored:
                    self._samples[metric] = restored
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self._samples.clear()
            self.baseline = BaselineEngine(
                max_samples=self.max_samples,
                min_samples=self._effective_min_samples,
            )

    def persist_system_state(self, system: str) -> None:
        """Atomically persist bounded metric samples for one SAP system."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "system": str(system),
            "samples": {
                metric: [
                    {"timestamp": sample.timestamp.isoformat(), "value": sample.value}
                    for sample in values[-self.max_samples:]
                ]
                for metric, values in self._samples.items()
            },
        }
        path = self._state_path(system)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _record_samples(self, result: MonitoringResult, now: datetime) -> None:
        for metric in result.metrics:
            try:
                value = float(metric.value)
            except (TypeError, ValueError):
                continue
            sample = MetricSample(now, metric.name, value)
            values = self._samples.setdefault(metric.name, [])
            values.append(sample)
            if len(values) > self.max_samples:
                del values[:-self.max_samples]

    def _all_samples(self) -> list[MetricSample]:
        output: list[MetricSample] = []
        for values in self._samples.values():
            output.extend(values)
        return output

    @staticmethod
    def _occurrences(system: str, rule_id: str) -> list[IncidentOccurrence]:
        occurrences = []
        for incident in load_incidents(system):
            if incident.rule_id != rule_id:
                continue
            occurrences.append(IncidentOccurrence(
                incident_id=incident.incident_id,
                rule_id=incident.rule_id,
                occurred_at=incident.first_seen,
                resolved=incident.status.value == "RESOLVED",
            ))
        return occurrences

    def evaluate(self, result: MonitoringResult, *, now: datetime | None = None) -> OperationalIntelligence:
        now = now or result.cycle_timestamp or datetime.now()
        baseline_results = self.baseline.evaluate_result(result)

        changes = detect_changes(
            self._all_samples(),
            as_of=now,
            lookback_seconds=self.change_lookback_seconds,
            min_samples=self.change_min_samples,
        )

        active = [
            i for i in result.incidents
            if getattr(getattr(i, "status", None), "value", i.status) != "RESOLVED"
        ]
        recurrence = None
        if active:
            # The highest-severity active incident is already selected by the
            # AI analyzer; use the first incident here to keep the coordinator
            # deterministic and side-effect free.
            rule_id = active[0].rule_id
            occurrences = self._occurrences(result.system, rule_id)
            for incident in active:
                occurrences.append(IncidentOccurrence(
                    incident_id=incident.incident_id,
                    rule_id=incident.rule_id,
                    occurred_at=incident.first_seen,
                    resolved=False,
                ))
            recurrence = assess_recurrence(
                occurrences,
                rule_id=rule_id,
                as_of=now,
                window_days=self.recurrence_window_days,
                min_occurrences=self.recurrence_min_occurrences,
            )

        intelligence = fuse_intelligence(
            baselines=[item.baseline for item in baseline_results.values()],
            changes=changes,
            recurrence=recurrence,
        )
        self._record_samples(result, now)
        # Persistence happens after evaluation so the current sample never
        # contaminates its own baseline/change calculation.
        try:
            self.persist_system_state(result.system)
        except Exception as exc:
            # Persistence is valuable but must never turn a successful
            # monitoring cycle into an application failure.
            log.error(
                "Milestone 8.2: intelligence state persistence failed for %s: %s",
                result.system,
                type(exc).__name__,
            )
        return intelligence


def attach_operational_intelligence(
    result: MonitoringResult,
    runtime: IntelligenceRuntime,
) -> OperationalIntelligence:
    """Evaluate and attach intelligence without changing metric/incident severity."""
    intelligence = runtime.evaluate(result)
    result.operational_intelligence = intelligence
    return intelligence


_RUNTIME_REGISTRY: dict[str, IntelligenceRuntime] = {}


def get_intelligence_runtime(system: str) -> IntelligenceRuntime:
    """Return the bounded intelligence runtime for one SAP system.

    The registry intentionally keys state by system so baseline samples are
    never mixed across SAP systems while repeated scheduler cycles retain
    historical samples.
    """
    key = str(system).strip()
    if not key:
        raise ValueError("system is required for intelligence runtime")
    runtime = _RUNTIME_REGISTRY.get(key)
    if runtime is None:
        runtime = IntelligenceRuntime()
        runtime.load_system_state(key)
        _RUNTIME_REGISTRY[key] = runtime
    return runtime


def reset_intelligence_runtime(system: str | None = None) -> None:
    """Test/support hook to clear one system or all runtime state."""
    if system is None:
        _RUNTIME_REGISTRY.clear()
    else:
        _RUNTIME_REGISTRY.pop(str(system).strip(), None)

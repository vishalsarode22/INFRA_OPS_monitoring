"""Safe integration layer between monitoring results and baseline intelligence.

This layer deliberately enriches monitoring observations without changing the
deterministic MetricResult.status. It can therefore be inserted into the
collection pipeline before events/correlation without changing severity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.baseline import BaselineSnapshot, calculate_baseline
from core.models import MetricResult, MonitoringResult


@dataclass(frozen=True)
class MetricIntelligence:
    metric: str
    baseline: BaselineSnapshot
    anomaly_candidate: bool
    reasons: tuple[str, ...]


class BaselineEngine:
    """In-memory rolling history for one monitoring process.

    The caller owns lifecycle/persistence. This avoids silently changing the
    existing incident persistence contract in the first integration step.
    """

    def __init__(self, max_samples: int = 240, min_samples: int = 5):
        if max_samples < min_samples:
            raise ValueError("max_samples must be >= min_samples")
        self.max_samples = max_samples
        self.min_samples = min_samples
        self._history: dict[str, list[float]] = {}

    def add_sample(self, metric: str, value) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return

        from math import isfinite
        if not isfinite(numeric):
            return

        values = self._history.setdefault(metric, [])
        values.append(numeric)
        if len(values) > self.max_samples:
            del values[:-self.max_samples]

    def history(self, metric: str) -> tuple[float, ...]:
        return tuple(self._history.get(metric, ()))

    def evaluate(self, metric: str, current) -> MetricIntelligence:
        history = self._history.get(metric, ())
        snapshot = calculate_baseline(
            metric,
            history,
            current=current,
            min_samples=self.min_samples,
        )

        # Import locally to keep baseline.py independent from the pipeline.
        from core.trend_engine import assess
        assessment = assess(snapshot)

        return MetricIntelligence(
            metric=metric,
            baseline=snapshot,
            anomaly_candidate=assessment.anomaly_candidate,
            reasons=assessment.reasons,
        )

    def evaluate_result(self, result: MonitoringResult) -> dict[str, MetricIntelligence]:
        """Evaluate the current result against history, then append samples.

        Evaluation happens BEFORE appending current values, preventing the
        current sample from contaminating its own baseline.
        """
        output: dict[str, MetricIntelligence] = {}

        for metric in result.metrics:
            if not isinstance(metric, MetricResult):
                continue
            output[metric.name] = self.evaluate(metric.name, metric.value)

        for metric in result.metrics:
            if isinstance(metric, MetricResult):
                self.add_sample(metric.name, metric.value)

        return output

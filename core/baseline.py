"""Deterministic rolling baseline, trend, and deviation calculations."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import mean, median, pstdev
from typing import Iterable


@dataclass(frozen=True)
class BaselineSnapshot:
    metric: str
    sample_count: int
    current: float | None
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None
    stddev: float | None
    p95: float | None
    trend: str
    rate_of_change: float | None
    deviation_percent: float | None
    z_score: float | None
    sufficient_history: bool


def _clean(values: Iterable[float]) -> list[float]:
    result = []
    for value in values:
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(value):
            result.append(value)
    return result


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _trend(values: list[float], min_samples: int = 5) -> str:
    # Do not infer operational trend until the baseline has enough history.
    if len(values) < min_samples:
        return "INSUFFICIENT_DATA"
    window = max(1, len(values) // 3)
    first = mean(values[:window])
    last = mean(values[-window:])
    scale = max(abs(mean(values)), 1e-9)
    delta = (last - first) / scale
    if delta > 0.05:
        return "INCREASING"
    if delta < -0.05:
        return "DECREASING"
    return "STABLE"


def calculate_baseline(
    metric: str,
    history: Iterable[float],
    current: float | None = None,
    min_samples: int = 5,
) -> BaselineSnapshot:
    values = _clean(history)
    if current is not None:
        try:
            current = float(current)
        except (TypeError, ValueError):
            current = None
        if current is not None and not isfinite(current):
            current = None

    sufficient = len(values) >= min_samples
    if not values:
        return BaselineSnapshot(
            metric=metric, sample_count=0, current=current,
            mean=None, median=None, minimum=None, maximum=None, stddev=None,
            p95=None, trend="INSUFFICIENT_DATA", rate_of_change=None,
            deviation_percent=None, z_score=None, sufficient_history=False,
        )

    avg = mean(values)
    std = pstdev(values) if len(values) > 1 else 0.0
    deviation = None
    z = None
    if current is not None and avg != 0:
        deviation = ((current - avg) / abs(avg)) * 100.0
    if current is not None and std > 0:
        z = (current - avg) / std

    roc = None
    if len(values) >= 2 and current is not None:
        previous = values[-1]
        if previous != 0:
            roc = ((current - previous) / abs(previous)) * 100.0

    return BaselineSnapshot(
        metric=metric,
        sample_count=len(values),
        current=current,
        mean=avg,
        median=median(values),
        minimum=min(values),
        maximum=max(values),
        stddev=std,
        p95=_percentile95(values),
        trend=_trend(values, min_samples=min_samples),
        rate_of_change=roc,
        deviation_percent=deviation,
        z_score=z,
        sufficient_history=sufficient,
    )

"""Detect material metric changes leading into an incident."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Iterable, Sequence


@dataclass(frozen=True)
class MetricSample:
    timestamp: datetime
    metric: str
    value: float


@dataclass(frozen=True)
class ChangeSignal:
    metric: str
    current_value: float
    baseline_value: float
    change_percent: float
    direction: str
    sample_count: int
    window_seconds: float
    significance: str


def _numeric(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _direction(change_percent: float) -> str:
    if change_percent > 5:
        return "INCREASED"
    if change_percent < -5:
        return "DECREASED"
    return "STABLE"


def detect_changes(
    samples: Iterable[MetricSample],
    *,
    as_of: datetime | None = None,
    lookback_seconds: int = 900,
    min_samples: int = 3,
    significance_percent: float = 25.0,
) -> list[ChangeSignal]:
    """Compare recent values with an earlier window.

    The comparison is intentionally simple and explainable:
    recent mean vs. prior mean. It is advisory and does not alter
    deterministic incident severity.
    """
    samples = sorted(samples, key=lambda s: s.timestamp)
    if not samples:
        return []

    end = as_of or samples[-1].timestamp
    start = end - timedelta(seconds=lookback_seconds)
    midpoint = start + timedelta(seconds=lookback_seconds / 2)

    grouped: dict[str, list[MetricSample]] = {}
    for sample in samples:
        if sample.timestamp > end or sample.timestamp < start:
            continue
        value = _numeric(sample.value)
        if value is None:
            continue
        grouped.setdefault(sample.metric, []).append(
            MetricSample(sample.timestamp, sample.metric, value)
        )

    signals: list[ChangeSignal] = []
    for metric, values in grouped.items():
        prior = [s.value for s in values if s.timestamp < midpoint]
        recent = [s.value for s in values if s.timestamp >= midpoint]
        if len(prior) < min_samples or len(recent) < min_samples:
            continue

        prior_mean = sum(prior) / len(prior)
        recent_mean = sum(recent) / len(recent)

        if prior_mean == 0:
            if recent_mean == 0:
                change = 0.0
            else:
                # Zero baselines cannot produce a meaningful percentage.
                continue
        else:
            change = ((recent_mean - prior_mean) / abs(prior_mean)) * 100.0

        direction = _direction(change)
        significance = (
            "MATERIAL" if abs(change) >= significance_percent
            else "MINOR"
        )
        signals.append(ChangeSignal(
            metric=metric,
            current_value=recent[-1],
            baseline_value=prior_mean,
            change_percent=round(change, 4),
            direction=direction,
            sample_count=len(values),
            window_seconds=lookback_seconds,
            significance=significance,
        ))

    signals.sort(key=lambda s: (-abs(s.change_percent), s.metric))
    return signals


def explain_change(signal: ChangeSignal) -> str:
    """Human-readable evidence suitable for dashboards and RCA context."""
    return (
        f"{signal.metric} {signal.direction.lower()} "
        f"by {abs(signal.change_percent):.1f}% "
        f"(prior mean {signal.baseline_value:.2f}, "
        f"current {signal.current_value:.2f}; "
        f"{signal.significance.lower()} change)."
    )

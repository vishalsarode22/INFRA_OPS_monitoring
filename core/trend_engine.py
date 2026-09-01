"""Operational interpretation of baseline snapshots."""
from __future__ import annotations

from dataclasses import dataclass

from core.baseline import BaselineSnapshot


@dataclass(frozen=True)
class TrendAssessment:
    metric: str
    state: str
    reasons: tuple[str, ...]
    anomaly_candidate: bool


def assess(snapshot: BaselineSnapshot, z_threshold: float = 3.0) -> TrendAssessment:
    if not snapshot.sufficient_history:
        return TrendAssessment(
            snapshot.metric, "INSUFFICIENT_DATA",
            ("Not enough clean historical samples for a baseline.",), False
        )

    reasons = []
    anomaly = False

    if snapshot.z_score is not None and abs(snapshot.z_score) >= z_threshold:
        reasons.append(f"z-score={snapshot.z_score:.2f}")
        anomaly = True

    if snapshot.deviation_percent is not None and abs(snapshot.deviation_percent) >= 25:
        reasons.append(f"deviation={snapshot.deviation_percent:.1f}%")
        anomaly = True

    if snapshot.trend != "STABLE":
        reasons.append(f"trend={snapshot.trend.lower()}")

    if snapshot.rate_of_change is not None and abs(snapshot.rate_of_change) >= 20:
        reasons.append(f"rate_of_change={snapshot.rate_of_change:.1f}%")
        anomaly = True

    state = "ANOMALY_CANDIDATE" if anomaly else "WITHIN_BASELINE"
    return TrendAssessment(snapshot.metric, state, tuple(reasons), anomaly)

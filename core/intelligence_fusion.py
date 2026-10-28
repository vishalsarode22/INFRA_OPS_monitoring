"""Fuse deterministic operational signals into one explainable intelligence object."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.baseline import BaselineSnapshot
from core.change_detector import ChangeSignal
from core.recurrence import RecurrenceAssessment


@dataclass(frozen=True)
class IntelligenceSignal:
    category: str
    metric: str
    strength: float
    summary: str


@dataclass(frozen=True)
class OperationalIntelligence:
    overall_signal: str
    score: float
    signals: tuple[IntelligenceSignal, ...]
    key_findings: tuple[str, ...]
    limitations: tuple[str, ...]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _baseline_signal(snapshot: BaselineSnapshot | None) -> IntelligenceSignal | None:
    if snapshot is None or not snapshot.sufficient_history:
        return None

    strength = 0.0
    reasons = []
    if snapshot.deviation_percent is not None:
        deviation = abs(snapshot.deviation_percent)
        strength = max(strength, _clamp(deviation / 100.0))
        if deviation >= 25:
            reasons.append(f"deviation {snapshot.deviation_percent:.1f}%")
    if snapshot.z_score is not None:
        strength = max(strength, _clamp(abs(snapshot.z_score) / 4.0))
        if abs(snapshot.z_score) >= 3:
            reasons.append(f"z-score {snapshot.z_score:.2f}")
    if snapshot.trend not in {"STABLE", "INSUFFICIENT_DATA"}:
        reasons.append(f"trend {snapshot.trend.lower()}")

    if not reasons:
        return None
    return IntelligenceSignal(
        "BASELINE", snapshot.metric, round(strength, 4),
        f"{snapshot.metric}: " + ", ".join(reasons),
    )


def _change_signal(signal: ChangeSignal | None) -> IntelligenceSignal | None:
    if signal is None or signal.significance != "MATERIAL":
        return None
    strength = _clamp(abs(signal.change_percent) / 100.0)
    return IntelligenceSignal(
        "PRE_INCIDENT_CHANGE", signal.metric, round(strength, 4),
        f"{signal.metric} {signal.direction.lower()} by "
        f"{abs(signal.change_percent):.1f}% before the observation window ended",
    )


def _recurrence_signal(assessment: RecurrenceAssessment | None) -> IntelligenceSignal | None:
    if assessment is None or not assessment.recurring:
        return None
    strength = _clamp(assessment.occurrence_count / 10.0)
    return IntelligenceSignal(
        "RECURRENCE", assessment.rule_id, round(strength, 4),
        f"{assessment.occurrence_count} occurrences in "
        f"{assessment.recurrence_window_days:.0f} days ({assessment.pattern.lower()})",
    )


def fuse_intelligence(
    *,
    baselines: Iterable[BaselineSnapshot] = (),
    changes: Iterable[ChangeSignal] = (),
    recurrence: RecurrenceAssessment | None = None,
) -> OperationalIntelligence:
    signals: list[IntelligenceSignal] = []
    limitations: list[str] = []

    for snapshot in baselines:
        signal = _baseline_signal(snapshot)
        if signal:
            signals.append(signal)
        elif not snapshot.sufficient_history:
            limitations.append(
                f"{snapshot.metric}: insufficient baseline history"
            )

    for change in changes:
        signal = _change_signal(change)
        if signal:
            signals.append(signal)

    signal = _recurrence_signal(recurrence)
    if signal:
        signals.append(signal)

    signals.sort(key=lambda item: (-item.strength, item.category, item.metric))

    # Weighted fusion: multiple independent signals increase confidence,
    # while repeated copies of the same category have diminishing impact.
    category_strength: dict[str, float] = {}
    for item in signals:
        category_strength[item.category] = max(
            category_strength.get(item.category, 0.0), item.strength
        )

    if not signals:
        return OperationalIntelligence(
            "NO_SIGNIFICANT_INTELLIGENCE", 0.0, (), (), tuple(limitations)
        )

    weighted_score = (
        0.45 * category_strength.get("BASELINE", 0.0)
        + 0.35 * category_strength.get("PRE_INCIDENT_CHANGE", 0.0)
        + 0.20 * category_strength.get("RECURRENCE", 0.0)
    )

    active_categories = sum(
        1 for value in category_strength.values() if value > 0
    )
    diversity_bonus = 0.0
    if active_categories >= 2:
        diversity_bonus = 0.30
    if active_categories >= 3:
        diversity_bonus = 0.35

    score = _clamp(weighted_score + diversity_bonus)

    if score >= 0.75:
        overall = "HIGH"
    elif score >= 0.45:
        overall = "MEDIUM"
    else:
        overall = "LOW"

    findings = tuple(item.summary for item in signals[:5])
    return OperationalIntelligence(
        overall_signal=overall,
        score=round(score, 4),
        signals=tuple(signals),
        key_findings=findings,
        limitations=tuple(limitations),
    )

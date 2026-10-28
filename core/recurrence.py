"""Explainable recurrence and repeat-problem intelligence."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Iterable


@dataclass(frozen=True)
class IncidentOccurrence:
    incident_id: str
    rule_id: str
    occurred_at: datetime
    resolved: bool = False
    confirmed_root_cause: str = ""
    resolution_verified: bool = False


@dataclass(frozen=True)
class RecurrenceAssessment:
    rule_id: str
    occurrence_count: int
    resolved_count: int
    recurrence_rate: float
    recurrence_window_days: float
    mean_interval_hours: float | None
    min_interval_hours: float | None
    max_interval_hours: float | None
    pattern: str
    recurring: bool
    verified_resolution_count: int
    repeated_root_causes: tuple[str, ...]


def _cause_key(value: str) -> str:
    words = re.findall(r"[a-z0-9_]{3,}", str(value).lower())
    return " ".join(words[:12])


def assess_recurrence(
    occurrences: Iterable[IncidentOccurrence],
    *,
    rule_id: str,
    as_of: datetime | None = None,
    window_days: int = 30,
    min_occurrences: int = 3,
) -> RecurrenceAssessment:
    end = as_of or datetime.now()
    start = end - timedelta(days=window_days)

    items = sorted(
        [
            x for x in occurrences
            if x.rule_id == rule_id and start <= x.occurred_at <= end
        ],
        key=lambda x: x.occurred_at,
    )

    count = len(items)
    resolved = sum(1 for x in items if x.resolved)
    verified = sum(1 for x in items if x.resolution_verified)

    if count < min_occurrences:
        return RecurrenceAssessment(
            rule_id=rule_id,
            occurrence_count=count,
            resolved_count=resolved,
            recurrence_rate=0.0,
            recurrence_window_days=float(window_days),
            mean_interval_hours=None,
            min_interval_hours=None,
            max_interval_hours=None,
            pattern="INSUFFICIENT_HISTORY",
            recurring=False,
            verified_resolution_count=verified,
            repeated_root_causes=(),
        )

    intervals = [
        (b.occurred_at - a.occurred_at).total_seconds() / 3600
        for a, b in zip(items, items[1:])
    ]
    mean_interval = sum(intervals) / len(intervals) if intervals else None

    recurrence_rate = count / max(float(window_days), 1.0) * 30.0

    if mean_interval is not None and mean_interval <= 24:
        pattern = "FREQUENT"
    elif mean_interval is not None and mean_interval <= 24 * 7:
        pattern = "WEEKLY_PATTERN"
    else:
        pattern = "RECURRING"

    causes = Counter(
        _cause_key(x.confirmed_root_cause)
        for x in items
        if x.confirmed_root_cause
    )
    repeated = tuple(
        cause for cause, occurrence_count in causes.most_common()
        if occurrence_count >= 2
    )

    return RecurrenceAssessment(
        rule_id=rule_id,
        occurrence_count=count,
        resolved_count=resolved,
        recurrence_rate=round(recurrence_rate, 3),
        recurrence_window_days=float(window_days),
        mean_interval_hours=round(mean_interval, 3) if mean_interval is not None else None,
        min_interval_hours=round(min(intervals), 3) if intervals else None,
        max_interval_hours=round(max(intervals), 3) if intervals else None,
        pattern=pattern,
        recurring=True,
        verified_resolution_count=verified,
        repeated_root_causes=repeated,
    )


def explain_recurrence(assessment: RecurrenceAssessment) -> str:
    if not assessment.recurring:
        return (
            f"{assessment.rule_id}: insufficient history "
            f"({assessment.occurrence_count} occurrence(s))."
        )

    text = (
        f"{assessment.rule_id}: {assessment.occurrence_count} occurrences "
        f"in {assessment.recurrence_window_days:.0f} days "
        f"({assessment.pattern.lower()})."
    )
    if assessment.mean_interval_hours is not None:
        text += f" Mean interval: {assessment.mean_interval_hours:.1f} hours."
    if assessment.repeated_root_causes:
        text += " Repeated confirmed cause(s): " + "; ".join(
            assessment.repeated_root_causes
        ) + "."
    return text

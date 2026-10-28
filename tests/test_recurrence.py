from datetime import datetime, timedelta

from core.recurrence import (
    IncidentOccurrence,
    assess_recurrence,
    explain_recurrence,
)


def occurrence(i, rule, when, resolved=True, cause="", verified=False):
    return IncidentOccurrence(
        incident_id=i,
        rule_id=rule,
        occurred_at=when,
        resolved=resolved,
        confirmed_root_cause=cause,
        resolution_verified=verified,
    )


def test_insufficient_history_is_not_recurring():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=10)),
        occurrence("2", "LOCK", now - timedelta(days=5)),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.recurring is False
    assert result.pattern == "INSUFFICIENT_HISTORY"


def test_three_occurrences_are_recurring():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=12)),
        occurrence("2", "LOCK", now - timedelta(days=6)),
        occurrence("3", "LOCK", now),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.recurring is True
    assert result.occurrence_count == 3
    assert result.pattern == "WEEKLY_PATTERN"


def test_frequent_pattern_is_detected():
    now = datetime(2026, 1, 10, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(hours=12)),
        occurrence("2", "LOCK", now - timedelta(hours=8)),
        occurrence("3", "LOCK", now - timedelta(hours=4)),
        occurrence("4", "LOCK", now),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.pattern == "FREQUENT"
    assert result.mean_interval_hours == 4


def test_unrelated_rules_are_excluded():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=2)),
        occurrence("2", "CPU", now - timedelta(days=1)),
        occurrence("3", "DB", now),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.recurring is False
    assert result.occurrence_count == 1


def test_future_occurrences_are_excluded():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=2)),
        occurrence("2", "LOCK", now - timedelta(days=1)),
        occurrence("3", "LOCK", now + timedelta(days=1)),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.occurrence_count == 2


def test_verified_resolution_count_is_reported():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=12), cause="batch lock", verified=True),
        occurrence("2", "LOCK", now - timedelta(days=6), cause="batch lock", verified=True),
        occurrence("3", "LOCK", now, cause="another cause", verified=False),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.verified_resolution_count == 2


def test_repeated_confirmed_cause_is_detected():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=12), cause="Batch process held enqueue lock"),
        occurrence("2", "LOCK", now - timedelta(days=6), cause="Batch process held enqueue lock"),
        occurrence("3", "LOCK", now, cause="Database issue"),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    assert result.repeated_root_causes == ("batch process held enqueue lock",)


def test_recurrence_rate_is_normalized_to_30_days():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=5)),
        occurrence("2", "LOCK", now - timedelta(days=3)),
        occurrence("3", "LOCK", now),
    ]
    result = assess_recurrence(
        items, rule_id="LOCK", as_of=now, window_days=10
    )
    assert result.recurrence_rate == 9.0


def test_explanation_is_human_readable():
    now = datetime(2026, 1, 30, 12, 0)
    items = [
        occurrence("1", "LOCK", now - timedelta(days=12), cause="batch lock"),
        occurrence("2", "LOCK", now - timedelta(days=6), cause="batch lock"),
        occurrence("3", "LOCK", now, cause="other"),
    ]
    result = assess_recurrence(items, rule_id="LOCK", as_of=now)
    text = explain_recurrence(result)
    assert "3 occurrences" in text
    assert "days" in text

from datetime import datetime, timedelta

from core.baseline import calculate_baseline
from core.change_detector import MetricSample, detect_changes
from core.intelligence_fusion import fuse_intelligence
from core.recurrence import IncidentOccurrence, assess_recurrence


def test_empty_inputs_are_safe():
    result = fuse_intelligence()
    assert result.overall_signal == "NO_SIGNIFICANT_INTELLIGENCE"
    assert result.score == 0.0
    assert result.signals == ()


def test_baseline_signal_is_fused():
    snapshot = calculate_baseline(
        "sap.st03n.response", [100, 101, 99, 100, 100], current=180
    )
    result = fuse_intelligence(baselines=[snapshot])
    assert result.overall_signal in {"MEDIUM", "HIGH"}
    assert any(x.category == "BASELINE" for x in result.signals)


def test_material_preincident_change_is_fused():
    start = datetime(2026, 1, 1, 10)
    samples = [
        MetricSample(start + timedelta(seconds=i * 60), "locks", value)
        for i, value in enumerate([5, 5, 5, 20, 20, 20])
    ]
    changes = detect_changes(samples, lookback_seconds=300, min_samples=2)
    result = fuse_intelligence(changes=changes)
    assert any(x.category == "PRE_INCIDENT_CHANGE" for x in result.signals)


def test_recurrence_is_fused():
    now = datetime(2026, 1, 30, 12)
    occurrences = [
        IncidentOccurrence("1", "LOCK", now - timedelta(days=12), True),
        IncidentOccurrence("2", "LOCK", now - timedelta(days=6), True),
        IncidentOccurrence("3", "LOCK", now, True),
    ]
    recurrence = assess_recurrence(occurrences, rule_id="LOCK", as_of=now)
    result = fuse_intelligence(recurrence=recurrence)
    assert any(x.category == "RECURRENCE" for x in result.signals)


def test_multiple_independent_signals_raise_overall_signal():
    snapshot = calculate_baseline(
        "locks", [10, 10, 11, 10, 10], current=30
    )
    now = datetime(2026, 1, 30, 12)
    occurrences = [
        IncidentOccurrence("1", "LOCK", now - timedelta(days=12), True),
        IncidentOccurrence("2", "LOCK", now - timedelta(days=6), True),
        IncidentOccurrence("3", "LOCK", now, True),
    ]
    recurrence = assess_recurrence(occurrences, rule_id="LOCK", as_of=now)
    result = fuse_intelligence(baselines=[snapshot], recurrence=recurrence)
    assert result.overall_signal == "HIGH"
    assert result.score >= 0.75


def test_insufficient_baseline_becomes_limitation_not_anomaly():
    snapshot = calculate_baseline("cpu", [10, 11], current=80, min_samples=5)
    result = fuse_intelligence(baselines=[snapshot])
    assert result.score == 0.0
    assert result.overall_signal == "NO_SIGNIFICANT_INTELLIGENCE"
    assert "insufficient baseline history" in result.limitations[0]


def test_duplicate_baseline_metrics_do_not_double_weight_category():
    a = calculate_baseline("cpu", [10, 10, 10, 10, 10], current=20)
    b = calculate_baseline("memory", [20, 20, 20, 20, 20], current=40)
    one = fuse_intelligence(baselines=[a])
    two = fuse_intelligence(baselines=[a, b])
    assert two.score == one.score


def test_key_findings_are_bounded():
    snapshots = [
        calculate_baseline(f"metric_{i}", [10, 10, 10, 10, 10], current=30)
        for i in range(10)
    ]
    result = fuse_intelligence(baselines=snapshots)
    assert len(result.key_findings) <= 5


def test_output_is_explainable():
    snapshot = calculate_baseline(
        "locks", [10, 11, 10, 12, 11], current=30
    )
    result = fuse_intelligence(baselines=[snapshot])
    assert result.signals[0].category == "BASELINE"
    assert result.signals[0].summary
    assert 0 <= result.score <= 1

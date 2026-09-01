from core.baseline import calculate_baseline
from core.trend_engine import assess


def test_insufficient_history_is_not_anomaly():
    snap = calculate_baseline("cpu", [10, 11, 12], current=40, min_samples=5)
    assert snap.sufficient_history is False
    assert snap.trend == "INSUFFICIENT_DATA"
    assert assess(snap).anomaly_candidate is False


def test_baseline_statistics_are_calculated():
    snap = calculate_baseline("cpu", [10, 20, 30, 40, 50], current=60)
    assert snap.sample_count == 5
    assert snap.mean == 30
    assert snap.median == 30
    assert snap.minimum == 10
    assert snap.maximum == 50
    assert snap.p95 > 45
    assert snap.deviation_percent == 100.0


def test_increasing_trend_is_detected():
    snap = calculate_baseline("locks", [5, 6, 8, 12, 18], current=20)
    assert snap.trend == "INCREASING"
    assert snap.rate_of_change is not None


def test_decreasing_trend_is_detected():
    snap = calculate_baseline("queue", [20, 18, 15, 12, 10], current=9)
    assert snap.trend == "DECREASING"


def test_stable_series_is_stable():
    snap = calculate_baseline("cpu", [50, 51, 49, 50, 50], current=51)
    assert snap.trend == "STABLE"


def test_large_deviation_becomes_anomaly_candidate():
    snap = calculate_baseline("response", [100, 102, 98, 101, 99, 100], current=180)
    assessment = assess(snap)
    assert assessment.anomaly_candidate is True
    assert "deviation=" in " ".join(assessment.reasons)


def test_z_score_can_detect_outlier():
    snap = calculate_baseline("locks", [10, 11, 10, 12, 11, 10, 11], current=30)
    assert snap.z_score is not None
    assert assess(snap).anomaly_candidate is True


def test_missing_and_non_finite_values_are_ignored():
    snap = calculate_baseline("cpu", [10, None, "bad", float("nan"), 20], current=25)
    assert snap.sample_count == 2
    assert snap.mean == 15
    assert snap.sufficient_history is False


def test_zero_baseline_does_not_divide_by_zero():
    snap = calculate_baseline("errors", [0, 0, 0, 0, 0], current=5)
    assert snap.deviation_percent is None
    assert snap.z_score is None
    assert snap.sufficient_history is True


def test_current_none_is_supported():
    snap = calculate_baseline("memory", [50, 51, 52, 49, 50], current=None)
    assert snap.current is None
    assert snap.deviation_percent is None
    assert snap.trend in {"STABLE", "INCREASING", "DECREASING"}

from datetime import datetime, timedelta

from core.change_detector import MetricSample, detect_changes, explain_change


def series(metric, values, start, step=60):
    return [
        MetricSample(start + timedelta(seconds=i * step), metric, value)
        for i, value in enumerate(values)
    ]


def test_detects_material_increase_before_incident():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("sap.sm12.lock_count", [5, 6, 5, 6, 7, 12, 18, 22, 25, 27], start)
    signals = detect_changes(samples, lookback_seconds=540, min_samples=3)
    signal = signals[0]
    assert signal.metric == "sap.sm12.lock_count"
    assert signal.direction == "INCREASED"
    assert signal.significance == "MATERIAL"
    assert signal.change_percent > 100


def test_detects_material_decrease():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("queue", [100, 98, 101, 99, 60, 55, 52, 50, 48, 45], start)
    signals = detect_changes(samples, lookback_seconds=540, min_samples=3)
    assert signals[0].direction == "DECREASED"
    assert signals[0].significance == "MATERIAL"


def test_minor_change_is_not_material():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("cpu", [50, 51, 49, 50, 52, 51, 50, 52, 51, 50], start)
    signals = detect_changes(samples, lookback_seconds=540, min_samples=3)
    assert signals[0].significance == "MINOR"


def test_insufficient_prior_or_recent_samples_is_ignored():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("locks", [5, 6, 7, 20], start)
    assert detect_changes(samples, lookback_seconds=180, min_samples=3) == []


def test_unknown_values_do_not_poison_detection():
    start = datetime(2026, 1, 1, 10, 0, 0)
    raw = series("response", [100, 101, 99, 100, 150, 160, 170, 180, 190, 200], start)
    raw.insert(3, MetricSample(start + timedelta(seconds=150), "response", float("nan")))
    signals = detect_changes(raw, lookback_seconds=540, min_samples=3)
    assert signals[0].significance == "MATERIAL"


def test_zero_baseline_is_not_falsely_reported_as_percentage():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("errors", [0, 0, 0, 1, 2, 3], start)
    assert detect_changes(samples, lookback_seconds=300, min_samples=2) == []


def test_multiple_metrics_are_sorted_by_absolute_change():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = (
        series("cpu", [50, 50, 50, 60, 60, 60], start)
        + series("locks", [10, 10, 10, 30, 30, 30], start)
    )
    signals = detect_changes(samples, lookback_seconds=300, min_samples=2)
    assert [s.metric for s in signals] == ["locks", "cpu"]


def test_as_of_excludes_future_samples():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("cpu", [50, 50, 50, 100, 100, 100], start)
    as_of = start + timedelta(seconds=120)
    assert detect_changes(samples, as_of=as_of, lookback_seconds=120, min_samples=2) == []


def test_change_explanation_is_dashboard_safe():
    start = datetime(2026, 1, 1, 10, 0, 0)
    samples = series("locks", [5, 5, 5, 10, 10, 10], start)
    signal = detect_changes(samples, lookback_seconds=300, min_samples=2)[0]
    text = explain_change(signal)
    assert "locks increased" in text
    assert "%" in text

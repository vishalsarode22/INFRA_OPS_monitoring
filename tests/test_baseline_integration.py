from core.baseline_integration import BaselineEngine
from core.models import MetricResult, MonitoringResult, Status


def result(value):
    return MonitoringResult(
        system="BASE-PRD",
        client="000",
        metrics=[
            MetricResult(
                name="sap.st03n.dialog_response_time",
                value=value,
                display_value=f"{value} ms",
                status=Status.NORMAL,
            )
        ],
    )


def test_baseline_engine_does_not_change_metric_severity():
    engine = BaselineEngine(min_samples=3)
    monitoring = result(180)
    before = monitoring.metrics[0].status

    intelligence = engine.evaluate_result(monitoring)

    assert monitoring.metrics[0].status == before
    assert intelligence["sap.st03n.dialog_response_time"].baseline.current == 180


def test_current_sample_is_not_used_in_its_own_baseline():
    engine = BaselineEngine(min_samples=3)
    for value in (100, 105, 95):
        engine.evaluate_result(result(value))

    intelligence = engine.evaluate_result(result(200))
    snap = intelligence["sap.st03n.dialog_response_time"].baseline

    assert snap.sample_count == 3
    assert snap.mean == 100
    assert snap.current == 200


def test_baseline_anomaly_is_exposed_without_becoming_critical():
    engine = BaselineEngine(min_samples=5)
    for value in (100, 102, 98, 101, 99):
        engine.evaluate_result(result(value))

    monitoring = result(180)
    intelligence = engine.evaluate_result(monitoring)

    item = intelligence["sap.st03n.dialog_response_time"]
    assert item.anomaly_candidate is True
    assert monitoring.metrics[0].status == Status.NORMAL


def test_insufficient_history_is_safe():
    engine = BaselineEngine(min_samples=5)
    intelligence = engine.evaluate_result(result(500))
    item = intelligence["sap.st03n.dialog_response_time"]

    assert item.baseline.sufficient_history is False
    assert item.anomaly_candidate is False


def test_unknown_or_non_numeric_samples_do_not_poison_history():
    engine = BaselineEngine(min_samples=3)
    engine.evaluate_result(result(100))
    bad = MonitoringResult(
        system="BASE-PRD",
        client="000",
        metrics=[
            MetricResult(
                name="sap.st03n.dialog_response_time",
                value="UNKNOWN",
                display_value="UNKNOWN",
                status=Status.UNKNOWN,
            )
        ],
    )
    engine.evaluate_result(bad)
    assert engine.history("sap.st03n.dialog_response_time") == (100.0,)


def test_history_is_capped():
    engine = BaselineEngine(max_samples=3, min_samples=2)
    for value in (1, 2, 3, 4, 5):
        engine.evaluate_result(result(value))
    assert engine.history("sap.st03n.dialog_response_time") == (3.0, 4.0, 5.0)


def test_multiple_metrics_are_evaluated_independently():
    engine = BaselineEngine(min_samples=2)
    monitoring = MonitoringResult(
        system="BASE-PRD",
        client="000",
        metrics=[
            MetricResult(name="cpu", value=20, display_value="20%", status=Status.NORMAL),
            MetricResult(name="locks", value=5, display_value="5", status=Status.NORMAL),
        ],
    )
    intelligence = engine.evaluate_result(monitoring)
    assert set(intelligence) == {"cpu", "locks"}


def test_engine_rejects_invalid_window_configuration():
    try:
        BaselineEngine(max_samples=2, min_samples=3)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models import MetricResult, MonitoringResult, Status
from core.tcode_metrics import normalize_tcode_result
from evaluation.threshold_engine import evaluate_all


def test_st22_is_normalized_and_thresholded():
    evidence = MetricResult(
        name="screenshot_ST22",
        value=None,
        display_value="captured (2)",
        status=Status.UNKNOWN,
        source="sap_gui_collector",
        tcode="ST22",
        extra_data={"dump_count": 4},
    )

    metrics = normalize_tcode_result(evidence)
    metrics = evaluate_all(metrics, {"sap.st22.dump_count": {"warning": 1, "critical": 10}})

    assert len(metrics) == 1
    assert metrics[0].name == "sap.st22.dump_count"
    assert metrics[0].value == 4
    assert metrics[0].status == Status.WARNING
    assert metrics[0].unit == "count"


def test_sm12_metric_becomes_critical():
    evidence = MetricResult(
        name="screenshot_SM12",
        value=None,
        display_value="captured (1)",
        status=Status.UNKNOWN,
        tcode="SM12",
        extra_data={"lock_count": 30},
    )

    metrics = normalize_tcode_result(evidence)
    metrics = evaluate_all(metrics, {"sap.sm12.lock_count": {"warning": 10, "critical": 25}})

    assert metrics[0].status == Status.CRITICAL


def test_all_unknown_does_not_look_healthy():
    result = MonitoringResult(system="TST", client="000")
    result.metrics.append(
        MetricResult(name="sap.st22.dump_count", value=None, display_value="n/a", status=Status.UNKNOWN)
    )
    assert result.compute_overall_status() == Status.UNKNOWN

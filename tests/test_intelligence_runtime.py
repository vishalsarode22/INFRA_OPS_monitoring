from datetime import datetime, timedelta

from core.intelligence_runtime import IntelligenceRuntime
from core.models import MetricResult, MonitoringResult, Status


def result(value=20):
    r = MonitoringResult(system="RUNTIME", client="000", cycle_timestamp=datetime(2026,1,30,12))
    r.metrics=[MetricResult("locks", value, str(value), Status.WARNING)]
    r.overall_status=Status.WARNING
    return r


def test_runtime_evaluates_without_changing_metric_status():
    r=result()
    runtime=IntelligenceRuntime()
    intelligence=runtime.evaluate(r)
    assert r.metrics[0].status == Status.WARNING
    assert intelligence is not None


def test_first_sample_is_insufficient_baseline():
    r=result(20)
    intelligence=IntelligenceRuntime().evaluate(r)
    assert intelligence.overall_signal == "NO_SIGNIFICANT_INTELLIGENCE"
    assert intelligence.limitations


def test_current_sample_is_not_used_in_own_baseline():
    runtime=IntelligenceRuntime(min_baseline_samples=2)
    for value in (10,10,10):
        runtime.evaluate(result(value))
    intelligence=runtime.evaluate(result(40))
    assert intelligence.overall_signal in {"MEDIUM", "HIGH"}


def test_runtime_history_is_bounded():
    runtime=IntelligenceRuntime(max_samples=3, min_baseline_samples=2)
    for value in range(10):
        runtime.evaluate(result(value))
    assert len(runtime._samples["locks"]) == 3


def test_runtime_does_not_change_incident_severity():
    r=result()
    class I:
        severity="CRITICAL"
        rule_id="LOCK"
        incident_id="I"
        first_seen=datetime(2026,1,30,11)
        status=type("S", (), {"value":"ACTIVE"})()
    r.incidents=[I()]
    IntelligenceRuntime().evaluate(r)
    assert r.incidents[0].severity == "CRITICAL"


def test_attach_is_explicit_and_returns_same_object():
    from core.intelligence_runtime import attach_operational_intelligence
    r=result()
    intelligence=attach_operational_intelligence(r, IntelligenceRuntime())
    assert r.operational_intelligence is intelligence

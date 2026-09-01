from core.models import MetricResult, MonitoringResult, Status
from core.event_engine import EventEngine
from core.correlation import CorrelationEngine
from evaluation.ai_analyzer import analyze
from evaluation.providers.mock import MockAIProvider


def _result_with_lock_incident(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    result = MonitoringResult(system="AI-TEST", client="100")
    result.metrics = [
        MetricResult("sap.sm12.lock_count", 23, "23", Status.CRITICAL, source="SAP_GUI"),
        MetricResult("sap.st03n.dialog_response_time", 780, "780 ms", Status.WARNING, source="SAP_GUI"),
        MetricResult("sap.st22.dump_count", 4, "4", Status.WARNING, source="SAP_GUI"),
        MetricResult("cpu", 18, "18%", Status.NORMAL, source="SSH"),
    ]
    result.compute_overall_status()
    result.events = EventEngine(result.system, result.client).process_metrics(result.metrics)
    result.incidents = CorrelationEngine(result.system, result.client).correlate(result.metrics, result.events)
    return result


def test_llm_context_is_incident_focused(tmp_path, monkeypatch):
    result = _result_with_lock_incident(tmp_path, monkeypatch)
    assert result.incidents
    analysis = analyze(result, provider=MockAIProvider(), incident=result.incidents[0])
    assert analysis.root_cause_category == "MONITORING_REVIEW"
    assert analysis.confidence_score == 0.60


def test_ai_fallback_preserves_incident_severity(tmp_path, monkeypatch):
    result = _result_with_lock_incident(tmp_path, monkeypatch)

    class BrokenProvider:
        def generate(self, prompt):
            raise RuntimeError("provider unavailable")

    analysis = analyze(result, provider=BrokenProvider(), incident=result.incidents[0])
    assert analysis.severity == result.incidents[0].severity
    assert analysis.root_cause_category == "AI_UNAVAILABLE"

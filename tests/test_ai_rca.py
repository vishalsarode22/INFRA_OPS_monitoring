from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze
from evaluation.ai_context import build_rca_context
from evaluation.ai_schemas import parse_json_response
from evaluation.ai_sanitizer import sanitize_text
from evaluation.providers.mock import MockAIProvider


def _result():
    result = MonitoringResult(system="TST", client="100")
    result.metrics.append(
        MetricResult(
            name="sap.sm12.lock_count",
            value=23,
            display_value="23",
            status=Status.CRITICAL,
            detail="Active locks",
        )
    )
    result.metrics.append(
        MetricResult(
            name="sap.st03n.dialog_response_time",
            value=780,
            display_value="780 ms",
            status=Status.WARNING,
            detail="Dialog response",
        )
    )
    result.compute_overall_status()
    return result


def test_context_prioritizes_sap_evidence():
    context = build_rca_context(_result())
    assert context["metrics"][0]["name"] == "sap.sm12.lock_count"


def test_secret_sanitizer_removes_common_credentials():
    text = "password=SuperSecret token=abc123 Authorization: Bearer abc.def"
    clean = sanitize_text(text)
    assert "SuperSecret" not in clean
    assert "abc123" not in clean
    assert "Bearer abc.def" not in clean


def test_structured_response_is_validated():
    raw = """```json
    {"severity":"CRITICAL",
     "root_cause":{"category":"SAP_LOCK_CONTENTION","description":"Lock contention"},
     "confidence":"HIGH","confidence_score":1.2,
     "supporting_evidence":["SM12=23"],
     "contradicting_evidence":[],
     "recommended_actions":["Review SM12"],
     "limitations":[]}
    ```"""
    parsed = parse_json_response(raw)
    assert parsed.severity == "CRITICAL"
    assert parsed.root_cause_category == "SAP_LOCK_CONTENTION"
    assert parsed.confidence_score == 1.0


def test_mock_provider_runs_without_network():
    result = _result()
    analysis = analyze(result, provider=MockAIProvider())
    assert analysis.severity == "CRITICAL"
    assert analysis.root_cause_category == "MONITORING_REVIEW"
    assert analysis.confidence_score == 0.60


def test_provider_selection_preserves_legacy_mock_switch(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    monkeypatch.setenv("USE_MOCK_AI", "true")
    from evaluation.ai_analyzer import get_provider
    assert get_provider().name == "mock"

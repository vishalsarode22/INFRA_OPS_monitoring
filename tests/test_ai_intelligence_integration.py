from datetime import datetime, timedelta

from core.ai_intelligence_context import build_ai_intelligence_context
from core.intelligence_fusion import (
    IntelligenceSignal,
    OperationalIntelligence,
)
from core.incidents import Incident, IncidentStatus
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze
from evaluation.providers.base import AIProvider


class InspectProvider(AIProvider):
    name = "inspect"

    def __init__(self):
        self.prompt = ""

    def generate(self, prompt):
        self.prompt = prompt
        return (
            '{"severity":"WARNING",'
            '"root_cause":{"category":"TEST","description":"Synthetic RCA"},'
            '"confidence":"MEDIUM","confidence_score":0.5,'
            '"supporting_evidence":["Observed test evidence"],'
            '"contradicting_evidence":[],"recommended_actions":["Inspect"],'
            '"limitations":[]}'
        )


def _intelligence():
    return OperationalIntelligence(
        overall_signal="HIGH",
        score=0.88,
        signals=(
            IntelligenceSignal(
                "BASELINE", "sap.sm12.lock_count", 0.9,
                "lock count deviation 180%"
            ),
            IntelligenceSignal(
                "RECURRENCE", "SAP_LOCK_CONTENTION", 0.6,
                "3 occurrences in 30 days"
            ),
        ),
        key_findings=(
            "lock count deviation 180%",
            "3 occurrences in 30 days",
        ),
        limitations=("Synthetic test evidence",),
    )


def _result():
    now = datetime(2026, 1, 30, 12)
    result = MonitoringResult(system="AI-INT", client="100")
    result.metrics = [
        MetricResult(
            "sap.sm12.lock_count", 23, "23", Status.WARNING, source="SAP_GUI"
        )
    ]
    result.overall_status = Status.WARNING
    result.incidents = [
        Incident(
            incident_id="INC-AI-001",
            system="AI-INT",
            client="100",
            rule_id="SAP_LOCK_CONTENTION",
            title="SAP lock contention",
            severity="WARNING",
            status=IncidentStatus.ACTIVE,
            first_seen=now - timedelta(minutes=5),
            last_seen=now,
            affected_metrics=["sap.sm12.lock_count"],
            evidence=["sap.sm12.lock_count=23"],
            confidence=0.8,
        )
    ]
    return result


def test_intelligence_is_included_in_existing_rca_prompt():
    provider = InspectProvider()
    analysis = analyze(
        _result(),
        provider=provider,
        incident=_result().incidents[0],
        intelligence=_intelligence(),
    )
    assert analysis.severity == "WARNING"
    assert "OPERATIONAL INTELLIGENCE" in provider.prompt
    assert '"overall_signal": "HIGH"' in provider.prompt
    assert '"score": 0.88' in provider.prompt
    assert "lock count deviation 180%" in provider.prompt


def test_intelligence_is_optional_for_legacy_callers():
    provider = InspectProvider()
    result = _result()
    analysis = analyze(result, provider=provider, incident=result.incidents[0])
    assert analysis.severity == "WARNING"
    assert "operational_intelligence" not in provider.prompt


def test_intelligence_does_not_override_authoritative_severity():
    class CriticalProvider(InspectProvider):
        def generate(self, prompt):
            self.prompt = prompt
            return (
                '{"severity":"CRITICAL",'
                '"root_cause":{"category":"TEST","description":"Synthetic RCA"},'
                '"confidence":"MEDIUM","confidence_score":0.5,'
                '"supporting_evidence":[],"contradicting_evidence":[],'
                '"recommended_actions":[],"limitations":[]}'
            )

    provider = CriticalProvider()
    result = _result()
    analysis = analyze(
        result,
        provider=provider,
        incident=result.incidents[0],
        intelligence=_intelligence(),
    )
    assert analysis.severity == "WARNING"


def test_intelligence_guardrail_is_present_in_prompt():
    provider = InspectProvider()
    result = _result()
    analyze(
        result,
        provider=provider,
        incident=result.incidents[0],
        intelligence=_intelligence(),
    )
    assert "authoritative incident severity" in provider.prompt
    assert "confirmed root cause" in provider.prompt


def test_context_adapter_and_analyzer_use_same_score():
    intelligence = _intelligence()
    context = build_ai_intelligence_context(intelligence)
    provider = InspectProvider()
    result = _result()
    analyze(
        result,
        provider=provider,
        incident=result.incidents[0],
        intelligence=intelligence,
    )
    assert f'"score": {context.score}' in provider.prompt


def test_intelligence_is_structured_json_not_flattened_text_only():
    provider = InspectProvider()
    result = _result()
    analyze(
        result,
        provider=provider,
        incident=result.incidents[0],
        intelligence=_intelligence(),
    )
    assert '"findings": [' in provider.prompt
    assert '"limitations": [' in provider.prompt

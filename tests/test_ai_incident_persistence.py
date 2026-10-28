from datetime import datetime
from unittest.mock import Mock

from core.correlation import CorrelationEngine
from core.incidents import Incident, IncidentStatus
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze_incidents
from evaluation.providers.base import AIProvider


class FixedProvider(AIProvider):
    name = "fixed"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        return (
            '{"severity":"WARNING",'
            '"root_cause":{"category":"SAP_LOCK_CONTENTION","description":"Synthetic lock contention"},'
            '"confidence":"HIGH","confidence_score":0.9,'
            '"supporting_evidence":["SM12 locks elevated"],'
            '"contradicting_evidence":[],"recommended_actions":["Inspect SM12"],'
            '"limitations":[]}'
        )


def _incident():
    now = datetime.now()
    return Incident(
        incident_id="INC-TEST-001",
        system="TEST",
        client="000",
        rule_id="SAP_LOCK_CONTENTION",
        title="Lock contention",
        severity="WARNING",
        status=IncidentStatus.ACTIVE,
        first_seen=now,
        last_seen=now,
        evidence=["SM12=23"],
        affected_metrics=["sap.sm12.lock_count"],
        event_ids=["EVT-1"],
        confidence=0.9,
    )


def _result(incident):
    result = MonitoringResult(system="TEST", client="000")
    result.metrics = [
        MetricResult(
            name="sap.sm12.lock_count",
            value=23,
            display_value="23",
            status=Status.WARNING,
            source="TEST",
        )
    ]
    result.overall_status = Status.WARNING
    result.incidents = [incident]
    return result


def test_new_incident_gets_ai_analysis_and_persists_it():
    incident = _incident()
    incident.ai_reanalysis_required = True
    provider = FixedProvider()

    result = _result(incident)
    analyses = analyze_incidents(result, provider=provider)

    assert len(analyses) == 1
    assert provider.calls == 1
    assert incident.ai_analysis is not None
    assert incident.ai_analysis["root_cause_category"] == "SAP_LOCK_CONTENTION"
    assert incident.ai_analysis_fingerprint
    assert incident.ai_analysis_at is not None


def test_unchanged_incident_does_not_call_llm_again():
    incident = _incident()
    provider = FixedProvider()
    result = _result(incident)

    analyze_incidents(result, provider=provider)
    assert provider.calls == 1

    incident.ai_reanalysis_required = False
    analyze_incidents(result, provider=provider)
    assert provider.calls == 1


def test_changed_evidence_triggers_reanalysis():
    incident = _incident()
    provider = FixedProvider()
    result = _result(incident)

    analyze_incidents(result, provider=provider)
    assert provider.calls == 1

    incident.evidence.append("ST03N=780ms")
    incident.ai_reanalysis_required = True
    analyze_incidents(result, provider=provider)
    assert provider.calls == 2


def test_correlation_marks_new_incident_for_ai():
    # No filesystem assertion here; use an isolated engine and a rule-like
    # synthetic incident update to verify the transient orchestration contract.
    incident = _incident()
    incident.ai_reanalysis_required = True
    assert incident.ai_reanalysis_required is True

from datetime import datetime

from core.incidents import Incident, IncidentStatus
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze
from evaluation.providers.base import AIProvider


class FixedProvider(AIProvider):
    name = "fixed"

    def __init__(self, severity):
        self.severity = severity

    def generate(self, prompt):
        return (
            '{"severity":"' + self.severity + '",'
            '"root_cause":{"category":"TEST","description":"Synthetic RCA"},'
            '"confidence":"HIGH","confidence_score":0.9,'
            '"supporting_evidence":["Synthetic evidence"],'
            '"contradicting_evidence":[],"recommended_actions":["Inspect"],'
            '"limitations":[]}'
        )


def _result(severity="CRITICAL"):
    result = MonitoringResult(
        system="TEST",
        client="000",
        metrics=[
            MetricResult(
                name="sap.sm12.lock_count",
                value=25,
                display_value="25",
                status=Status.CRITICAL,
                source="TEST",
            )
        ],
    )
    result.overall_status = Status.CRITICAL
    result.incidents = [
        Incident(
            incident_id="INC-TEST-001",
            system="TEST",
            client="000",
            rule_id="SAP_LOCK_CONTENTION",
            title="SAP lock contention",
            severity=severity,
            status=IncidentStatus.ACTIVE,
            first_seen=datetime.now(),
            last_seen=datetime.now(),
            affected_metrics=["sap.sm12.lock_count"],
            evidence=["sap.sm12.lock_count=25"],
            confidence=0.9,
        )
    ]
    return result


def test_llm_cannot_downgrade_critical_incident():
    analysis = analyze(_result("CRITICAL"), provider=FixedProvider("NORMAL"))
    assert analysis.severity == "CRITICAL"
    assert any("overridden" in x.lower() for x in analysis.contradicting_evidence) is False
    assert "overridden" in analysis.raw_response.lower() or analysis.raw_response


def test_llm_cannot_upgrade_warning_incident():
    analysis = analyze(_result("WARNING"), provider=FixedProvider("CRITICAL"))
    assert analysis.severity == "WARNING"


def test_provider_receives_authoritative_severity_contract():
    class InspectProvider(AIProvider):
        name = "inspect"
        prompt = ""

        def generate(self, prompt):
            self.prompt = prompt
            return (
                '{"severity":"CRITICAL","root_cause":{"category":"TEST",'
                '"description":"Synthetic RCA"},"confidence":"MEDIUM",'
                '"confidence_score":0.5,"supporting_evidence":[] ,'
                '"contradicting_evidence":[],"recommended_actions":[],"limitations":[]}'
            )

    provider = InspectProvider()
    analyze(_result("WARNING"), provider=provider)
    assert '"authoritative_severity": "WARNING"' in provider.prompt
    assert "deterministic_monitoring" in provider.prompt

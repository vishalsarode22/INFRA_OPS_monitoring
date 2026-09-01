
from datetime import datetime

from core.incidents import Incident, IncidentStatus


def test_incident_ai_fields_round_trip():
    now = datetime.now()
    incident = Incident(
        incident_id="INC-1",
        system="TEST",
        client="000",
        rule_id="RULE",
        title="Test",
        severity="WARNING",
        status=IncidentStatus.ACTIVE,
        first_seen=now,
        last_seen=now,
        ai_analysis={
            "severity": "WARNING",
            "root_cause_category": "TEST",
            "likely_root_cause": "Synthetic",
        },
        ai_analysis_at=now,
        ai_analysis_fingerprint="abc123",
    )
    restored = Incident.from_dict(incident.to_dict())
    assert restored.ai_analysis["root_cause_category"] == "TEST"
    assert restored.ai_analysis_fingerprint == "abc123"
    assert restored.ai_analysis_at is not None

from datetime import datetime

import pytest

from core.incidents import Incident, IncidentStatus
from core.incident_actions import acknowledge_incident, investigate_incident, resolve_incident
from core.resolutions import ResolutionCategory


def incident(status=IncidentStatus.ACTIVE):
    now = datetime(2026, 1, 30, 12)
    return Incident("A-1", "SYS", "000", "LOCK", "Lock", "CRITICAL",
                    status, now, now)


def test_acknowledge_records_operator_action_without_changing_severity():
    inc = incident()
    acknowledge_incident(inc, actor="admin")
    assert inc.status == IncidentStatus.ACKNOWLEDGED
    assert inc.severity == "CRITICAL"
    assert inc.acknowledged_by == "admin"
    assert inc.action_history[-1]["action"] == "ACKNOWLEDGE"


def test_investigate_can_follow_acknowledgement():
    inc = incident()
    acknowledge_incident(inc)
    investigate_incident(inc, actor="basis")
    assert inc.status == IncidentStatus.INVESTIGATING
    assert [x["action"] for x in inc.action_history] == ["ACKNOWLEDGE", "INVESTIGATE"]


def test_resolve_requires_summary_and_creates_verified_operator_record():
    inc = incident(IncidentStatus.INVESTIGATING)
    resolve_incident(
        inc, actor="basis", summary="Released blocking process",
        category=ResolutionCategory.APPLICATION_PROCESS,
        actions_taken=["Stopped batch job"],
        confirmed_root_cause="Batch job held enqueue lock",
        verified=True,
    )
    assert inc.status == IncidentStatus.RESOLVED
    assert inc.resolution is not None
    assert inc.resolution.verified is True
    assert inc.action_history[-1]["action"] == "RESOLVE"


def test_resolved_incident_cannot_be_acknowledged_or_investigated():
    inc = incident(IncidentStatus.RESOLVED)
    with pytest.raises(ValueError):
        acknowledge_incident(inc)
    with pytest.raises(ValueError):
        investigate_incident(inc)


def test_resolve_cannot_be_repeated():
    inc = incident(IncidentStatus.RESOLVED)
    with pytest.raises(ValueError, match="already"):
        resolve_incident(inc, summary="already resolved")

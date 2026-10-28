from datetime import datetime

import pytest

from core.incidents import Incident, IncidentStatus
from core.incident_store import load_incidents, save_incidents
from core.resolutions import ResolutionCategory, confirm_resolution


def _incident(status=IncidentStatus.RESOLVED):
    now = datetime.now()
    return Incident(
        incident_id="INC-RES-1",
        system="RES-PRD",
        client="000",
        rule_id="SAP_LOCK_CONTENTION",
        title="Lock contention",
        severity="WARNING",
        status=status,
        first_seen=now,
        last_seen=now,
        resolved_at=now,
        evidence=["SM12 lock count = 20"],
    )


def test_resolution_requires_resolved_incident():
    incident = _incident(IncidentStatus.ACTIVE)
    with pytest.raises(ValueError, match="RESOLVED"):
        confirm_resolution(
            incident,
            category=ResolutionCategory.USER_ACTION,
            summary="Released blocking session",
        )


def test_ai_hypothesis_is_not_confirmed_root_cause():
    incident = _incident()
    incident.ai_analysis = {
        "root_cause_category": "LOCK_CONTENTION",
        "likely_root_cause": "Long-held application lock",
    }
    assert incident.resolution is None

    record = confirm_resolution(
        incident,
        category=ResolutionCategory.APPLICATION_PROCESS,
        summary="Terminated blocking batch process",
        actions_taken=["Identified APP_BATCH", "Terminated blocking process"],
        confirmed_root_cause="Blocking batch process held the enqueue lock",
        verified=True,
        resolver="basis-admin",
    )

    assert record.confirmed_root_cause != incident.ai_analysis["likely_root_cause"]
    assert record.verified is True


def test_resolution_round_trip(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    incident = _incident()
    confirm_resolution(
        incident,
        category=ResolutionCategory.APPLICATION_PROCESS,
        summary="Stopped blocking process",
        actions_taken=["Stopped process"],
        confirmed_root_cause="Batch process held lock",
        verified=True,
        resolver="admin",
    )

    save_incidents("RES-PRD", [incident])
    loaded = load_incidents("RES-PRD")[0]

    assert loaded.resolution is not None
    assert loaded.resolution.category == ResolutionCategory.APPLICATION_PROCESS
    assert loaded.resolution.confirmed_root_cause == "Batch process held lock"
    assert loaded.resolution.verified is True


def test_resolution_cannot_be_empty():
    incident = _incident()
    with pytest.raises(ValueError, match="summary"):
        confirm_resolution(
            incident,
            category=ResolutionCategory.UNKNOWN,
            summary=" ",
        )

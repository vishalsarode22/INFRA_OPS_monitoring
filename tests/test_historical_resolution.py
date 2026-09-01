from datetime import datetime

from core.history import find_similar_incidents
from core.incident_store import save_incidents
from core.incidents import Incident, IncidentStatus
from core.resolutions import ResolutionCategory, confirm_resolution


def _incident(iid):
    now = datetime.now()
    return Incident(
        incident_id=iid,
        system="HIST-RES",
        client="000",
        rule_id="SAP_LOCK_CONTENTION",
        title="Lock contention",
        severity="WARNING",
        status=IncidentStatus.RESOLVED,
        first_seen=now,
        last_seen=now,
        resolved_at=now,
        affected_metrics=["sap.sm12.lock_count"],
        evidence=["SM12 lock count = 20", "blocking batch process"],
        confidence=0.9,
        ai_analysis={
            "root_cause_category": "LOCK_CONTENTION",
            "likely_root_cause": "Application lock",
            "recommended_actions": ["Inspect SM12"],
        },
    )


def test_historical_match_exposes_verified_outcome(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    old = _incident("OLD-RES")
    confirm_resolution(
        old,
        category=ResolutionCategory.APPLICATION_PROCESS,
        summary="Terminated blocking batch process",
        actions_taken=["Identify lock owner", "Terminate batch process"],
        confirmed_root_cause="Batch process held the enqueue lock",
        verified=True,
        resolver="basis-admin",
    )
    save_incidents("HIST-RES", [old])

    current = _incident("NEW-RES")
    current.status = IncidentStatus.ACTIVE
    current.resolved_at = None

    matches = find_similar_incidents("HIST-RES", current)
    assert len(matches) == 1
    assert matches[0].resolution_verified is True
    assert matches[0].confirmed_root_cause == "Batch process held the enqueue lock"
    assert matches[0].resolution_summary == "Terminated blocking batch process"
    assert "Terminate batch process" in matches[0].resolution_actions

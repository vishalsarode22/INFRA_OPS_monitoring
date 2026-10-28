from datetime import datetime

from core.incidents import Incident, IncidentStatus
from core.incident_store import save_incidents
from dashboard import intelligence_api


def make_incident():
    now = datetime(2026, 1, 30, 12)
    return Incident("API-ACT-1", "API-SYS", "000", "LOCK", "Lock", "WARNING",
                    IncidentStatus.ACTIVE, now, now)


def test_incident_action_api_persists_acknowledgement(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))
    monkeypatch.setattr(intelligence_api, "list_snapshot_systems", lambda: ["API-SYS"])
    save_incidents("API-SYS", [make_incident()])
    payload = intelligence_api.incident_action(
        "API-ACT-1",
        intelligence_api.IncidentActionRequest(action="ACKNOWLEDGE", actor="tester"),
    )
    assert payload["success"] is True
    assert payload["severity_authoritative"] is True
    assert payload["incident"]["status"] == "ACKNOWLEDGED"
    assert payload["incident"]["severity"] == "WARNING"


def test_incident_action_api_resolves_with_operator_outcome(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))
    monkeypatch.setattr(intelligence_api, "list_snapshot_systems", lambda: ["API-SYS"])
    save_incidents("API-SYS", [make_incident()])
    payload = intelligence_api.incident_action(
        "API-ACT-1",
        intelligence_api.IncidentActionRequest(
            action="RESOLVE", actor="tester", summary="Released blocking session",
            confirmed_root_cause="Blocking session held lock", verified=True,
        ),
    )
    assert payload["incident"]["status"] == "RESOLVED"
    assert payload["incident"]["resolution"]["verified"] is True

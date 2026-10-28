from datetime import datetime, timedelta
import json

from core.history import find_similar_incidents
from core.incidents import Incident, IncidentStatus
from core.incident_store import save_incidents
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_context import build_rca_context


def _incident(iid, rule, evidence, resolved=True, ai=None):
    now = datetime.now()
    return Incident(
        incident_id=iid,
        system="HIST-PRD",
        client="000",
        rule_id=rule,
        title=rule,
        severity="WARNING",
        status=IncidentStatus.RESOLVED if resolved else IncidentStatus.ACTIVE,
        first_seen=now - timedelta(hours=2),
        last_seen=now,
        event_ids=[f"EV-{iid}"],
        evidence=evidence,
        affected_metrics=["sap.sm12.lock_count", "sap.st03n.dialog_response_time"],
        confidence=0.8,
        description="test",
        resolved_at=now if resolved else None,
        ai_analysis=ai,
    )


def test_similar_resolved_incident_is_retrieved(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    old = _incident(
        "OLD-1",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 20", "ST03N response = 700 ms"],
        ai={
            "root_cause_category": "LOCK_CONTENTION",
            "likely_root_cause": "Long-held SAP locks",
            "recommended_actions": ["Inspect SM12"],
        },
    )
    save_incidents("HIST-PRD", [old])

    current = _incident(
        "NEW-1",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 25", "ST03N response = 780 ms"],
        resolved=False,
    )

    matches = find_similar_incidents("HIST-PRD", current)
    assert len(matches) == 1
    assert matches[0].incident_id == "OLD-1"
    assert matches[0].root_cause_category == "LOCK_CONTENTION"


def test_unrelated_rule_is_not_returned(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    old = _incident(
        "OLD-2",
        "SAP_PROCESS_FAILURE",
        ["sap_process_disp=CRITICAL"],
    )
    save_incidents("HIST-PRD", [old])

    current = _incident(
        "NEW-2",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 25"],
        resolved=False,
    )
    assert find_similar_incidents("HIST-PRD", current) == []


def test_active_history_is_excluded(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    active = _incident(
        "ACTIVE-1",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 25"],
        resolved=False,
    )
    save_incidents("HIST-PRD", [active])

    current = _incident(
        "NEW-3",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 30"],
        resolved=False,
    )
    assert find_similar_incidents("HIST-PRD", current) == []


def test_rca_context_contains_historical_matches(tmp_path, monkeypatch):
    import core.incident_store as store
    monkeypatch.setattr(store, "INCIDENT_DIR", str(tmp_path))

    old = _incident(
        "OLD-4",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 20", "ST03N response = 700 ms"],
        ai={
            "root_cause_category": "LOCK_CONTENTION",
            "likely_root_cause": "Long-held locks",
            "recommended_actions": ["Inspect SM12"],
        },
    )
    save_incidents("HIST-PRD", [old])

    current = _incident(
        "NEW-4",
        "SAP_LOCK_CONTENTION",
        ["SM12 lock count = 25", "ST03N response = 780 ms"],
        resolved=False,
    )
    result = MonitoringResult(
        system="HIST-PRD",
        client="000",
        metrics=[
            MetricResult(
                name="sap.sm12.lock_count",
                value=25,
                display_value="25",
                status=Status.WARNING,
            )
        ],
    )
    result.overall_status = Status.WARNING
    result.incidents = [current]

    context = build_rca_context(result, current)
    assert len(context["historical_matches"]) == 1
    assert context["historical_matches"][0]["root_cause_category"] == "LOCK_CONTENTION"

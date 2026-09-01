from datetime import datetime

from core.correlation import CorrelationEngine, load_correlation_config
from core.event_engine import EventEngine
from core.incidents import IncidentStatus
from core.models import MetricResult, Status


def metric(name, value, status, unit="count"):
    return MetricResult(name=name, value=value, display_value=f"{value} {unit}", status=status, unit=unit)


def test_rules_are_loaded_from_configuration():
    config = load_correlation_config()
    assert "SAP_LOCK_CONTENTION" in config
    assert config["SAP_LOCK_CONTENTION"]["thresholds"]["lock_count"] == 10


def test_rule_specific_event_evidence_is_not_polluted(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    now = datetime(2026, 8, 18, 12, 0, 0)
    metrics = [
        metric("sap.sm12.lock_count", 20, Status.WARNING),
        metric("sap.st03n.dialog_response_time", 700, Status.WARNING, "ms"),
        metric("cpu", 95, Status.CRITICAL, "%"),
        metric("memory", 92, Status.CRITICAL, "%"),
    ]
    events = EventEngine("PRD", "100").process_metrics(metrics, now)
    incidents = CorrelationEngine("PRD", "100").correlate(metrics, events, now)
    lock = next(i for i in incidents if i.rule_id == "SAP_LOCK_CONTENTION")
    assert all("cpu" not in evidence.lower() for evidence in lock.evidence)
    assert all("memory" not in evidence.lower() for evidence in lock.evidence)


def test_acknowledged_incident_remains_acknowledged_on_next_match(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    first = datetime(2026, 8, 18, 12, 0, 0)
    metrics = [
        metric("sap.sm12.lock_count", 20, Status.WARNING),
        metric("sap.st03n.dialog_response_time", 700, Status.WARNING, "ms"),
    ]
    engine = EventEngine("QAS", "100")
    events = engine.process_metrics(metrics, first)
    corr = CorrelationEngine("QAS", "100")
    incidents = corr.correlate(metrics, events, first)
    active = next(i for i in incidents if i.rule_id == "SAP_LOCK_CONTENTION")
    corr.acknowledge(active.incident_id, "basis-admin", first)

    events = engine.process_metrics(metrics, datetime(2026, 8, 18, 12, 5, 0))
    incidents = corr.correlate(metrics, events, datetime(2026, 8, 18, 12, 5, 0))
    current = next(i for i in incidents if i.incident_id == active.incident_id)
    assert current.status == IncidentStatus.ACKNOWLEDGED
    assert current.acknowledged_by == "basis-admin"


def test_resolved_rule_creates_new_occurrence_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    t1 = datetime(2026, 8, 18, 12, 0, 0)
    bad = [
        metric("sap.sm12.lock_count", 20, Status.WARNING),
        metric("sap.st03n.dialog_response_time", 700, Status.WARNING, "ms"),
    ]
    good = [
        metric("sap.sm12.lock_count", 1, Status.NORMAL),
        metric("sap.st03n.dialog_response_time", 200, Status.NORMAL, "ms"),
    ]
    engine = EventEngine("DEV", "100")
    corr = CorrelationEngine("DEV", "100")
    events = engine.process_metrics(bad, t1)
    first = corr.correlate(bad, events, t1)
    first_id = next(i.incident_id for i in first if i.rule_id == "SAP_LOCK_CONTENTION")
    events = engine.process_metrics(good, t1.replace(minute=5))
    corr.correlate(good, events, t1.replace(minute=5))
    events = engine.process_metrics(bad, t1.replace(minute=10))
    incidents = corr.correlate(bad, events, t1.replace(minute=10))
    active = next(i for i in incidents if i.rule_id == "SAP_LOCK_CONTENTION" and i.status != IncidentStatus.RESOLVED)
    assert active.incident_id != first_id

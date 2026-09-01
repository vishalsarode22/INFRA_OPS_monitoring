from datetime import datetime

from core.correlation import CorrelationEngine
from core.event_engine import EventEngine
from core.incidents import IncidentStatus
from core.models import MetricResult, Status


def metric(name, value, status, unit="count"):
    return MetricResult(name=name, value=value, display_value=f"{value} {unit}", status=status, unit=unit)


def test_lock_contention_creates_correlated_incident(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    now = datetime(2026, 8, 18, 12, 0, 0)
    metrics = [
        metric("sap.sm12.lock_count", 20, Status.WARNING),
        metric("sap.st03n.dialog_response_time", 700, Status.WARNING, "ms"),
        metric("sap.st22.dump_count", 2, Status.WARNING),
        metric("cpu", 25, Status.NORMAL, "%"),
    ]
    events = EventEngine("TST", "100").process_metrics(metrics, now)
    incidents = CorrelationEngine("TST", "100").correlate(metrics, events, now)
    active = [i for i in incidents if i.status != IncidentStatus.RESOLVED]
    assert len(active) == 1
    assert active[0].rule_id == "SAP_LOCK_CONTENTION"
    assert active[0].confidence >= 0.8
    assert any(item.startswith("sap.sm12.lock_count=20 count") for item in active[0].evidence)


def test_infrastructure_pressure_creates_incident(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    now = datetime(2026, 8, 18, 12, 0, 0)
    metrics = [
        metric("cpu", 95, Status.CRITICAL, "%"),
        metric("memory", 92, Status.CRITICAL, "%"),
        metric("load_1m", 9, Status.CRITICAL),
    ]
    events = EventEngine("PRD", "100").process_metrics(metrics, now)
    incidents = CorrelationEngine("PRD", "100").correlate(metrics, events, now)
    assert any(i.rule_id == "INFRA_RESOURCE_PRESSURE" and i.status != IncidentStatus.RESOLVED for i in incidents)


def test_process_failure_creates_incident(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    now = datetime(2026, 8, 18, 12, 0, 0)
    metrics = [metric("sap_process_disp+work", 1, Status.CRITICAL, "state")]
    events = EventEngine("QAS", "100").process_metrics(metrics, now)
    incidents = CorrelationEngine("QAS", "100").correlate(metrics, events, now)
    assert any(i.rule_id == "SAP_PROCESS_FAILURE" for i in incidents)


def test_incident_resolves_when_correlation_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr("core.incident_store.INCIDENT_DIR", str(tmp_path / "incidents"))
    now = datetime(2026, 8, 18, 12, 0, 0)
    bad = [
        metric("sap.sm12.lock_count", 20, Status.WARNING),
        metric("sap.st03n.dialog_response_time", 700, Status.WARNING, "ms"),
        metric("sap.st22.dump_count", 1, Status.WARNING),
    ]
    engine = EventEngine("DEV", "100")
    events = engine.process_metrics(bad, now)
    corr = CorrelationEngine("DEV", "100")
    incidents = corr.correlate(bad, events, now)
    assert any(i.rule_id == "SAP_LOCK_CONTENTION" and i.status != IncidentStatus.RESOLVED for i in incidents)

    good = [
        metric("sap.sm12.lock_count", 2, Status.NORMAL),
        metric("sap.st03n.dialog_response_time", 200, Status.NORMAL, "ms"),
        metric("sap.st22.dump_count", 0, Status.NORMAL),
    ]
    events = engine.process_metrics(good, datetime(2026, 8, 18, 12, 5, 0))
    incidents = corr.correlate(good, events, datetime(2026, 8, 18, 12, 5, 0))
    resolved = [i for i in incidents if i.rule_id == "SAP_LOCK_CONTENTION"]
    assert resolved[0].status == IncidentStatus.RESOLVED

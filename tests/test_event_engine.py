from datetime import datetime, timedelta

from core.event_engine import EventEngine
from core.events import EventStatus
from core.models import MetricResult, Status


def metric(name="cpu", status=Status.CRITICAL, value=95, ts=None):
    return MetricResult(
        name=name,
        value=value,
        display_value=f"{value}%",
        status=status,
        threshold_warning=80,
        threshold_critical=90,
        timestamp=ts or datetime.now(),
    )


def test_repeated_critical_samples_are_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path))
    engine = EventEngine("TST", "000")
    t1 = datetime(2026, 8, 18, 12, 0, 0)
    t2 = t1 + timedelta(minutes=5)

    first = engine.process_metrics([metric(ts=t1)], now=t1)
    second = engine.process_metrics([metric(ts=t2)], now=t2)

    active = [e for e in second if e.status == EventStatus.ACTIVE]
    assert len(active) == 1
    assert active[0].occurrences == 2
    assert active[0].first_seen == t1
    assert active[0].last_seen == t2


def test_normal_metric_resolves_existing_event(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path))
    engine = EventEngine("TST", "000")
    t1 = datetime(2026, 8, 18, 12, 0, 0)
    t2 = t1 + timedelta(minutes=5)

    engine.process_metrics([metric(ts=t1)], now=t1)
    events = engine.process_metrics([metric(status=Status.NORMAL, value=45, ts=t2)], now=t2)

    assert len(events) == 1
    assert events[0].status == EventStatus.RESOLVED
    assert events[0].resolved_at == t2
    assert events[0].occurrences == 1


def test_severity_change_updates_same_event(tmp_path, monkeypatch):
    monkeypatch.setattr("core.event_store.EVENT_DIR", str(tmp_path))
    engine = EventEngine("TST", "000")
    t1 = datetime(2026, 8, 18, 12, 0, 0)
    t2 = t1 + timedelta(minutes=5)

    engine.process_metrics([metric(status=Status.WARNING, value=85, ts=t1)], now=t1)
    events = engine.process_metrics([metric(status=Status.CRITICAL, value=95, ts=t2)], now=t2)

    assert len(events) == 1
    assert events[0].severity == Status.CRITICAL
    assert events[0].occurrences == 2
    assert events[0].event_id.startswith("EVT-")

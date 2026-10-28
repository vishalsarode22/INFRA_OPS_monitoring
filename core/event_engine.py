"""Deterministic event lifecycle engine.

Rules:
- WARNING/CRITICAL metrics create or update an ACTIVE event.
- Repeated observations update the same event and increment occurrences.
- A NORMAL/UNKNOWN observation resolves an existing event for that metric.
- A severity change on an active event updates the current severity without
  creating duplicate events.

The engine does not send email and does not call an LLM. Those are consumers
of the resulting event/incident state.
"""

from datetime import datetime
from typing import Iterable

from core.event_store import load_events, save_events
from core.events import EventStatus, MonitoringEvent, event_from_metric, event_key
from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__, "monitoring")


class EventEngine:
    """Update durable event state from one monitoring cycle."""

    def __init__(self, system: str, client: str):
        self.system = system
        self.client = client
        self.events = load_events(system)

    def process_metrics(self, metrics: Iterable[MetricResult], now: datetime | None = None) -> list[MonitoringEvent]:
        now = now or datetime.now()
        by_key = {event_key(e.system, e.metric_name): e for e in self.events}

        observed_keys: set[str] = set()
        for metric in metrics:
            key = event_key(self.system, metric.name)
            observed_keys.add(key)
            existing = by_key.get(key)

            if metric.status in (Status.WARNING, Status.CRITICAL):
                if existing and existing.status == EventStatus.ACTIVE:
                    existing.last_seen = now
                    existing.occurrences += 1
                    existing.current_value = metric.value
                    existing.display_value = metric.display_value
                    existing.severity = metric.status
                    existing.threshold_warning = metric.threshold_warning
                    existing.threshold_critical = metric.threshold_critical
                    existing.source = metric.source
                    existing.tcode = metric.tcode
                    existing.detail = metric.detail
                    existing.resolved_at = None
                elif existing and existing.status == EventStatus.RESOLVED:
                    existing.status = EventStatus.ACTIVE
                    existing.severity = metric.status
                    existing.last_seen = now
                    existing.occurrences += 1
                    existing.current_value = metric.value
                    existing.display_value = metric.display_value
                    existing.threshold_warning = metric.threshold_warning
                    existing.threshold_critical = metric.threshold_critical
                    existing.source = metric.source
                    existing.tcode = metric.tcode
                    existing.detail = metric.detail
                    existing.resolved_at = None
                else:
                    event = event_from_metric(self.system, self.client, metric, now)
                    by_key[key] = event
                    log.warning(
                        f"New {metric.status.value} event {event.event_id}: "
                        f"{self.system}/{metric.name}={metric.display_value}"
                    )
            elif existing and existing.status == EventStatus.ACTIVE:
                existing.status = EventStatus.RESOLVED
                existing.last_seen = now
                existing.resolved_at = now
                existing.current_value = metric.value
                existing.display_value = metric.display_value
                log.info(f"Resolved event {existing.event_id}: {self.system}/{metric.name}")

        # Persist the complete history. Events are intentionally retained after
        # resolution so later incident/history layers can inspect them.
        self.events = sorted(by_key.values(), key=lambda e: e.last_seen, reverse=True)
        save_events(self.system, self.events)
        return self.events

    def active_events(self) -> list[MonitoringEvent]:
        return [e for e in self.events if e.status == EventStatus.ACTIVE]

    def resolved_events(self) -> list[MonitoringEvent]:
        return [e for e in self.events if e.status == EventStatus.RESOLVED]

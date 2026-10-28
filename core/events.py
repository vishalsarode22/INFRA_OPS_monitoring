"""Event models and deterministic event lifecycle for monitoring metrics.

An event represents a persistent condition, not a single metric sample.
The engine deliberately stays deterministic: thresholds decide whether a
metric is actionable; this module turns repeated observations into stable
ACTIVE/RESOLVED events. AI is not involved in event state decisions.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from core.models import MetricResult, Status


class EventStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


@dataclass
class MonitoringEvent:
    """A persistent condition identified from one normalized metric."""

    event_id: str
    system: str
    client: str
    metric_name: str
    severity: Status
    status: EventStatus
    first_seen: datetime
    last_seen: datetime
    occurrences: int = 1
    current_value: Optional[float] = None
    display_value: str = ""
    threshold_warning: Optional[float] = None
    threshold_critical: Optional[float] = None
    source: str = ""
    tcode: Optional[str] = None
    detail: str = ""
    resolved_at: Optional[datetime] = None

    @property
    def duration_seconds(self) -> float:
        end = self.resolved_at or self.last_seen
        return max(0.0, (end - self.first_seen).total_seconds())

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("first_seen", "last_seen", "resolved_at"):
            value = data.get(key)
            data[key] = value.isoformat() if value else None
        data["severity"] = self.severity.value
        data["status"] = self.status.value
        data["duration_seconds"] = self.duration_seconds
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MonitoringEvent":
        return cls(
            event_id=data["event_id"],
            system=data["system"],
            client=data["client"],
            metric_name=data["metric_name"],
            severity=Status(data["severity"]),
            status=EventStatus(data["status"]),
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
            occurrences=int(data.get("occurrences", 1)),
            current_value=data.get("current_value"),
            display_value=data.get("display_value", ""),
            threshold_warning=data.get("threshold_warning"),
            threshold_critical=data.get("threshold_critical"),
            source=data.get("source", ""),
            tcode=data.get("tcode"),
            detail=data.get("detail", ""),
            resolved_at=(
                datetime.fromisoformat(data["resolved_at"])
                if data.get("resolved_at") else None
            ),
        )


def event_key(system: str, metric_name: str) -> str:
    """Stable key used to deduplicate repeated observations."""
    return f"{system.strip()}::{metric_name.strip()}".lower()


def event_id_for(system: str, metric_name: str) -> str:
    """Human-readable stable event identifier."""
    import hashlib

    digest = hashlib.sha1(event_key(system, metric_name).encode("utf-8")).hexdigest()[:10].upper()
    return f"EVT-{digest}"


def event_from_metric(system: str, client: str, metric: MetricResult, now: datetime) -> MonitoringEvent:
    """Create a new event from an actionable metric."""
    return MonitoringEvent(
        event_id=event_id_for(system, metric.name),
        system=system,
        client=client,
        metric_name=metric.name,
        severity=metric.status,
        status=EventStatus.ACTIVE,
        first_seen=now,
        last_seen=now,
        occurrences=1,
        current_value=metric.value,
        display_value=metric.display_value,
        threshold_warning=metric.threshold_warning,
        threshold_critical=metric.threshold_critical,
        source=metric.source,
        tcode=metric.tcode,
        detail=metric.detail,
    )

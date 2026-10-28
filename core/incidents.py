"""Incident models for correlated SAP monitoring events."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from core.resolutions import ResolutionRecord


class IncidentStatus(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"


@dataclass
class Incident:
    """A correlated operational problem made up of one or more events."""

    incident_id: str
    system: str
    client: str
    rule_id: str
    title: str
    severity: str
    status: IncidentStatus
    first_seen: datetime
    last_seen: datetime
    event_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    affected_metrics: list[str] = field(default_factory=list)
    confidence: float = 0.0
    description: str = ""
    resolved_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    ai_analysis: Optional[dict] = None
    ai_analysis_at: Optional[datetime] = None
    ai_analysis_fingerprint: str = ""
    resolution: Optional[ResolutionRecord] = None
    action_history: list[dict] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        end = self.resolved_at or self.last_seen
        return max(0.0, (end - self.first_seen).total_seconds())

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in (
            "first_seen", "last_seen", "resolved_at", "acknowledged_at",
            "ai_analysis_at",
        ):
            value = data.get(key)
            data[key] = value.isoformat() if value else None
        data["status"] = self.status.value
        data["duration_seconds"] = self.duration_seconds
        if self.resolution is not None:
            data["resolution"] = self.resolution.to_dict()
        data["action_history"] = list(self.action_history[-50:])
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Incident":
        resolution_data = data.get("resolution")
        return cls(
            incident_id=data["incident_id"],
            system=data["system"],
            client=data["client"],
            rule_id=data["rule_id"],
            title=data["title"],
            severity=data.get("severity", "WARNING"),
            status=IncidentStatus(data.get("status", "ACTIVE")),
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
            event_ids=list(data.get("event_ids", [])),
            evidence=list(data.get("evidence", [])),
            affected_metrics=list(data.get("affected_metrics", [])),
            confidence=float(data.get("confidence", 0.0)),
            description=data.get("description", ""),
            resolved_at=(
                datetime.fromisoformat(data["resolved_at"])
                if data.get("resolved_at") else None
            ),
            acknowledged_at=(
                datetime.fromisoformat(data["acknowledged_at"])
                if data.get("acknowledged_at") else None
            ),
            acknowledged_by=data.get("acknowledged_by"),
            ai_analysis=data.get("ai_analysis"),
            ai_analysis_at=(
                datetime.fromisoformat(data["ai_analysis_at"])
                if data.get("ai_analysis_at") else None
            ),
            ai_analysis_fingerprint=data.get("ai_analysis_fingerprint", ""),
            resolution=(
                ResolutionRecord.from_dict(resolution_data)
                if resolution_data else None
            ),
            action_history=list(data.get("action_history", []))[-50:],
        )

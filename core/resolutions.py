"""Explicit, human-verifiable incident resolution records."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class ResolutionCategory(str, Enum):
    APPLICATION_PROCESS = "APPLICATION_PROCESS"
    SAP_CONFIGURATION = "SAP_CONFIGURATION"
    DATABASE = "DATABASE"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    NETWORK = "NETWORK"
    USER_ACTION = "USER_ACTION"
    TRANSIENT = "TRANSIENT"
    UNKNOWN = "UNKNOWN"


@dataclass
class ResolutionRecord:
    """Ground-truth operational outcome; never populated by the LLM alone."""

    resolved_at: datetime
    category: ResolutionCategory = ResolutionCategory.UNKNOWN
    summary: str = ""
    actions_taken: list[str] = field(default_factory=list)
    confirmed_root_cause: str = ""
    verified: bool = False
    resolver: str = ""
    source: str = "operator"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["resolved_at"] = self.resolved_at.isoformat()
        data["category"] = self.category.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ResolutionRecord":
        return cls(
            resolved_at=datetime.fromisoformat(data["resolved_at"]),
            category=ResolutionCategory(
                data.get("category", ResolutionCategory.UNKNOWN.value)
            ),
            summary=str(data.get("summary", "")),
            actions_taken=[str(x) for x in data.get("actions_taken", [])],
            confirmed_root_cause=str(data.get("confirmed_root_cause", "")),
            verified=bool(data.get("verified", False)),
            resolver=str(data.get("resolver", "")),
            source=str(data.get("source", "operator")),
        )


def confirm_resolution(
    incident,
    *,
    category: ResolutionCategory,
    summary: str,
    actions_taken: list[str] | None = None,
    confirmed_root_cause: str = "",
    verified: bool = True,
    resolver: str = "",
    source: str = "operator",
    resolved_at: datetime | None = None,
) -> ResolutionRecord:
    """Attach an explicit operational outcome to an incident.

    This function intentionally requires an explicit caller action. AI RCA
    cannot call this as a side effect of generating a hypothesis.
    """
    if incident.status.value != "RESOLVED":
        raise ValueError("Only RESOLVED incidents can receive a resolution record.")
    if not summary.strip():
        raise ValueError("Resolution summary is required.")
    if not source.strip():
        raise ValueError("Resolution source is required.")

    record = ResolutionRecord(
        resolved_at=resolved_at or incident.resolved_at or incident.last_seen,
        category=category,
        summary=summary.strip(),
        actions_taken=[str(x).strip() for x in (actions_taken or []) if str(x).strip()],
        confirmed_root_cause=confirmed_root_cause.strip(),
        verified=bool(verified),
        resolver=resolver.strip(),
        source=source.strip(),
    )
    incident.resolution = record
    return record

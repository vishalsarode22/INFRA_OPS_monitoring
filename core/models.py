"""
Shared data models used across the monitoring pipeline.

The models intentionally remain dependency-light so collectors, evaluators,
reporters and the API can share the same contracts.  T-code evidence is
represented as normal metrics as well as retaining the original screenshot
and extraction evidence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class Status(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"  # collector failed / no data


@dataclass
class MetricResult:
    """One normalized observation produced by a collector."""

    name: str
    value: Optional[float]
    display_value: str
    status: Status
    threshold_warning: Optional[float] = None
    threshold_critical: Optional[float] = None
    source: str = ""
    tcode: Optional[str] = None
    detail: str = ""
    screenshot_path: Optional[str] = None
    screenshot_paths: list[str] = field(default_factory=list)
    extra_data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    # New metadata used by the correlation/incident layers.  Defaults keep
    # existing callers and reports backwards compatible.
    category: str = ""
    unit: str = ""
    metric_type: str = "gauge"  # gauge, counter, state
    freshness_seconds: Optional[float] = None

    @property
    def is_actionable(self) -> bool:
        return self.status in (Status.WARNING, Status.CRITICAL)


@dataclass
class AIAnalysis:
    severity: str = ""
    likely_root_cause: str = ""
    evidence: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    confidence: str = ""
    raw_response: str = ""

    # Structured RCA metadata.
    root_cause_category: str = ""
    finding_status: str = "UNKNOWN"
    confidence_score: Optional[float] = None

    supporting_metrics: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class MonitoringResult:
    """The output of one full monitoring cycle for one SAP system/client."""

    system: str
    client: str
    cycle_timestamp: datetime = field(default_factory=datetime.now)
    metrics: list[MetricResult] = field(default_factory=list)
    overall_status: Status = Status.UNKNOWN
    ai_analysis: Optional[AIAnalysis] = None
    events: list = field(default_factory=list)
    incidents: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    operational_intelligence: Any = None

    def compute_overall_status(self) -> Status:
        """
        Compute health without treating missing data as healthy.

        Rules:
        - Any CRITICAL => CRITICAL
        - Else any WARNING => WARNING
        - Else if at least one NORMAL and no actionable status => NORMAL
        - Else (all UNKNOWN/no observations) => UNKNOWN
        """
        statuses = [m.status for m in self.metrics]
        if Status.CRITICAL in statuses:
            self.overall_status = Status.CRITICAL
        elif Status.WARNING in statuses:
            self.overall_status = Status.WARNING
        elif Status.NORMAL in statuses:
            self.overall_status = Status.NORMAL
        else:
            self.overall_status = Status.UNKNOWN
        return self.overall_status

    def critical_metrics(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == Status.CRITICAL]

    def warning_metrics(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == Status.WARNING]

    def unknown_metrics(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == Status.UNKNOWN]

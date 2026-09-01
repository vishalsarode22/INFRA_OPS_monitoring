"""Deterministic correlation rules for turning events into incidents.

Rules are configuration-driven so operational thresholds can be tuned without
changing Python code. Correlation remains deterministic and explainable; the
LLM is intentionally downstream of this layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

import yaml

from core.config_loader import CONFIG_DIR
from core.event_store import load_events
from core.events import EventStatus, MonitoringEvent
from core.incident_store import load_incidents, save_incidents
from core.incidents import Incident, IncidentStatus
from core.models import MetricResult, Status


CORRELATION_CONFIG_PATH = f"{CONFIG_DIR}/correlation_rules.yaml"


@dataclass(frozen=True)
class CorrelationRule:
    rule_id: str
    title: str
    severity: Status
    description: str
    matcher: Callable[[dict[str, MetricResult], list[MonitoringEvent], dict], bool]
    evidence_builder: Callable[[dict[str, MetricResult], list[MonitoringEvent], dict], list[str]]
    confidence: float
    enabled: bool = True
    config: dict | None = None


def load_correlation_config(path: str = CORRELATION_CONFIG_PATH) -> dict[str, dict]:
    """Load enabled correlation policy from YAML.

    A malformed or missing config is treated as an application configuration
    error rather than silently changing monitoring behavior.
    """
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    rules = data.get("rules") or []
    result: dict[str, dict] = {}
    for raw in rules:
        rule_id = str(raw.get("id", "")).strip()
        if rule_id:
            result[rule_id] = raw
    return result


def _metric(metrics: dict[str, MetricResult], name: str) -> MetricResult | None:
    return metrics.get(name)


def _above(metrics: dict[str, MetricResult], name: str, value: float) -> bool:
    item = _metric(metrics, name)
    return bool(item and item.value is not None and item.value > value)


def _has_event(events: list[MonitoringEvent], metric_name: str) -> bool:
    return any(
        e.status == EventStatus.ACTIVE and e.metric_name == metric_name
        for e in events
    )


def _lock_contention_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return (
        _above(metrics, "sap.sm12.lock_count", float(t.get("lock_count", 10)))
        and _above(metrics, "sap.st03n.dialog_response_time", float(t.get("response_time_ms", 500)))
        and (
            _above(metrics, "sap.st22.dump_count", float(t.get("dump_count", 0)))
            or _has_event(events, "sap.sm12.lock_count")
            or _has_event(events, "sap.st03n.dialog_response_time")
        )
    )


def _lock_contention_evidence(metrics, events, cfg):
    evidence = []
    for name in (
        "sap.sm12.lock_count",
        "sap.st03n.dialog_response_time",
        "sap.st22.dump_count",
    ):
        item = _metric(metrics, name)
        if item and item.value is not None:
            evidence.append(f"{name}={item.display_value} ({item.status.value})")
    return evidence


def _infra_pressure_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    thresholds = (
        ("cpu", float(t.get("cpu_percent", 90))),
        ("memory", float(t.get("memory_percent", 90))),
        ("load_1m", float(t.get("load_1m", 8))),
    )
    high_resources = sum(_above(metrics, name, threshold) for name, threshold in thresholds)
    minimum = int(t.get("minimum_high_resources", 2))
    response = float(t.get("response_time_ms", 1000))
    return high_resources >= minimum or (
        _above(metrics, "cpu", thresholds[0][1])
        and _above(metrics, "sap.st03n.dialog_response_time", response)
    )


def _infra_pressure_evidence(metrics, events, cfg):
    evidence = []
    for name in ("cpu", "memory", "load_1m", "sap.st03n.dialog_response_time"):
        item = _metric(metrics, name)
        if item and item.value is not None:
            evidence.append(f"{name}={item.display_value} ({item.status.value})")
    return evidence


def _process_failure_match(metrics, events, cfg):
    return any(
        e.status == EventStatus.ACTIVE
        and e.metric_name.startswith("sap_process_")
        and e.severity == Status.CRITICAL
        for e in events
    ) or any(
        m.status == Status.CRITICAL and m.name.startswith("sap_process_")
        for m in metrics.values()
    )


def _process_failure_evidence(metrics, events, cfg):
    return [
        f"{name}={metric.display_value} ({metric.status.value})"
        for name, metric in metrics.items()
        if name.startswith("sap_process_") and metric.status == Status.CRITICAL
    ]


_MATCHERS = {
    "SAP_LOCK_CONTENTION": (_lock_contention_match, _lock_contention_evidence),
    "INFRA_RESOURCE_PRESSURE": (_infra_pressure_match, _infra_pressure_evidence),
    "SAP_PROCESS_FAILURE": (_process_failure_match, _process_failure_evidence),
}


def get_correlation_rules(path: str = CORRELATION_CONFIG_PATH) -> tuple[CorrelationRule, ...]:
    config = load_correlation_config(path)
    rules = []
    for rule_id, raw in config.items():
        if not raw.get("enabled", True):
            continue
        functions = _MATCHERS.get(rule_id)
        if not functions:
            continue
        rules.append(
            CorrelationRule(
                rule_id=rule_id,
                title=str(raw.get("title", rule_id)),
                severity=Status(str(raw.get("severity", "WARNING")).upper()),
                description=str(raw.get("description", "")),
                matcher=functions[0],
                evidence_builder=functions[1],
                confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.5)))),
                enabled=True,
                config=raw,
            )
        )
    return tuple(rules)


# Backwards-compatible public constant used by existing callers/tests.
RULES = get_correlation_rules()


def incident_id_for(system: str, rule_id: str, occurrence: int = 1) -> str:
    """Stable incident identity for an active correlation occurrence."""
    key = f"{system.strip().lower()}::{rule_id.strip().lower()}::{occurrence}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10].upper()
    return f"INC-{digest}"


def _event_ids_for_rule(rule_id: str, metric_map: dict[str, MetricResult], events: list[MonitoringEvent]) -> list[str]:
    """Return only events relevant to the current correlation rule."""
    if rule_id == "SAP_LOCK_CONTENTION":
        names = {"sap.sm12.lock_count", "sap.st03n.dialog_response_time", "sap.st22.dump_count"}
    elif rule_id == "INFRA_RESOURCE_PRESSURE":
        names = {"cpu", "memory", "load_1m", "sap.st03n.dialog_response_time"}
    elif rule_id == "SAP_PROCESS_FAILURE":
        names = {name for name in metric_map if name.startswith("sap_process_")}
    else:
        names = set(metric_map)
    return sorted({
        event.event_id
        for event in events
        if event.status == EventStatus.ACTIVE and event.metric_name in names
    })


def _affected_metrics_for_rule(rule_id: str, metric_map: dict[str, MetricResult]) -> list[str]:
    if rule_id == "SAP_LOCK_CONTENTION":
        names = {"sap.sm12.lock_count", "sap.st03n.dialog_response_time", "sap.st22.dump_count"}
    elif rule_id == "INFRA_RESOURCE_PRESSURE":
        names = {"cpu", "memory", "load_1m", "sap.st03n.dialog_response_time"}
    elif rule_id == "SAP_PROCESS_FAILURE":
        names = {name for name in metric_map if name.startswith("sap_process_")}
    else:
        names = set(metric_map)
    return sorted(
        metric.name for name, metric in metric_map.items()
        if name in names and metric.status in (Status.WARNING, Status.CRITICAL)
    )


def _incident_state_fingerprint(
    severity: str,
    evidence: list[str],
    affected_metrics: list[str],
    event_ids: list[str],
) -> str:
    """Fingerprint evidence that materially changes an incident's RCA context."""
    payload = json.dumps(
        {
            "severity": str(severity).upper(),
            "evidence": sorted(set(evidence)),
            "affected_metrics": sorted(set(affected_metrics)),
            "event_ids": sorted(set(event_ids)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CorrelationEngine:
    """Evaluate deterministic rules and maintain durable incident state."""

    def __init__(self, system: str, client: str, rules: tuple[CorrelationRule, ...] | None = None):
        self.system = system
        self.client = client
        self.rules = rules if rules is not None else get_correlation_rules()
        self.incidents = load_incidents(system)

    def correlate(self, metrics, events=None, now=None) -> list[Incident]:
        now = now or datetime.now()
        metric_map = {metric.name: metric for metric in metrics}
        active_events = list(events) if events is not None else load_events(self.system)
        by_rule = {incident.rule_id: incident for incident in self.incidents}

        for rule in self.rules:
            matched = rule.matcher(metric_map, active_events, rule.config or {})
            existing = by_rule.get(rule.rule_id)
            evidence = rule.evidence_builder(metric_map, active_events, rule.config or {}) if matched else []
            event_ids = _event_ids_for_rule(rule.rule_id, metric_map, active_events) if matched else []
            affected_metrics = _affected_metrics_for_rule(rule.rule_id, metric_map) if matched else []

            if matched:
                if existing and existing.status != IncidentStatus.RESOLVED:
                    previous_fingerprint = _incident_state_fingerprint(
                        existing.severity,
                        existing.evidence,
                        existing.affected_metrics,
                        existing.event_ids,
                    )
                    previous_status = existing.status
                    existing.last_seen = now
                    if existing.status == IncidentStatus.NEW:
                        existing.status = IncidentStatus.ACTIVE
                    existing.severity = rule.severity.value
                    existing.event_ids = sorted(set(existing.event_ids) | set(event_ids))
                    existing.evidence = evidence
                    existing.affected_metrics = affected_metrics
                    existing.confidence = rule.confidence
                    existing.description = rule.description
                    existing.resolved_at = None

                    current_fingerprint = _incident_state_fingerprint(
                        existing.severity,
                        existing.evidence,
                        existing.affected_metrics,
                        existing.event_ids,
                    )
                    # This transient flag is intentionally not persisted.
                    # It tells the AI orchestration layer whether this cycle
                    # materially changed the incident's RCA context.
                    existing.ai_reanalysis_required = (
                        previous_fingerprint != current_fingerprint
                        or previous_status == IncidentStatus.NEW
                    )
                else:
                    # A resolved incident is a completed occurrence. Start a
                    # new identity rather than overwriting historical state.
                    previous_occurrences = sum(1 for i in self.incidents if i.rule_id == rule.rule_id)
                    incident = Incident(
                        incident_id=incident_id_for(self.system, rule.rule_id, previous_occurrences + 1),
                        system=self.system,
                        client=self.client,
                        rule_id=rule.rule_id,
                        title=rule.title,
                        severity=rule.severity.value,
                        status=IncidentStatus.ACTIVE,
                        first_seen=now,
                        last_seen=now,
                        event_ids=event_ids,
                        evidence=evidence,
                        affected_metrics=affected_metrics,
                        confidence=rule.confidence,
                        description=rule.description,
                    )
                    # New occurrence must receive an initial RCA.
                    incident.ai_reanalysis_required = True
                    by_rule[rule.rule_id] = incident
                    self.incidents.append(incident)
            elif existing and existing.status != IncidentStatus.RESOLVED:
                existing.status = IncidentStatus.RESOLVED
                existing.last_seen = now
                existing.resolved_at = now

        self.incidents = sorted(self.incidents, key=lambda incident: incident.last_seen, reverse=True)
        save_incidents(self.system, self.incidents)
        return self.incidents

    def active_incidents(self):
        return [i for i in self.incidents if i.status != IncidentStatus.RESOLVED]

    def resolved_incidents(self):
        return [i for i in self.incidents if i.status == IncidentStatus.RESOLVED]

    def acknowledge(self, incident_id: str, acknowledged_by: str, now=None):
        now = now or datetime.now()
        for incident in self.incidents:
            if incident.incident_id == incident_id and incident.status != IncidentStatus.RESOLVED:
                incident.status = IncidentStatus.ACKNOWLEDGED
                incident.acknowledged_at = now
                incident.acknowledged_by = acknowledged_by
                save_incidents(self.system, self.incidents)
                return incident
        return None

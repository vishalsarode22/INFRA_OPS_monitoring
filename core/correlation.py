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


def _value(metrics, name):
    item = _metric(metrics, name)
    return item.value if item and item.value is not None else None


def _evidence_for(metrics, names, extra=None):
    """Evidence lines for whichever of these metrics were actually read.

    A metric that could not be read contributes an explicit "not read" line
    rather than being dropped. An incident whose evidence silently omits the
    blind spots reads as better supported than it is.
    """
    lines = []
    for name in names:
        item = _metric(metrics, name)
        if item is None:
            continue
        if item.value is None and item.status is not Status.UNKNOWN:
            continue
        if item.status is Status.UNKNOWN:
            lines.append(f"{name}=not read ({item.source or 'no source'})")
        else:
            lines.append(f"{name}={item.display_value} ({item.status.value})")
            if item.detail:
                lines.append(f"  detail: {item.detail[:160]}")
    lines.extend(extra or [])
    return lines


# --- update failures -------------------------------------------------------
# Any failed V1 update is worth an incident. Unlike CPU, there is no healthy
# background level: a posting either committed or it did not.

def _update_failure_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return _above(metrics, "sap.sm13.failed_updates",
                  float(t.get("failed_updates", 1)) - 1)


def _update_failure_evidence(metrics, events, cfg):
    return _evidence_for(metrics, ("sap.sm13.failed_updates", "update_queue",
                                   "sap.sm50.wp_in_use"))


# --- background jobs -------------------------------------------------------

def _batch_failure_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return _above(metrics, "sap.sm37.cancelled_jobs",
                  float(t.get("cancelled_jobs", 1)) - 1)


def _batch_failure_evidence(metrics, events, cfg):
    """
    Per-job detail, not a count.

    "3 cancelled jobs" tells nobody what to do. Which job, run by whom, at
    what time, for how long, and whether it started at all is the difference
    between an authorisation problem and an ABAP failure -- and those are
    different people's work.
    """
    extra = []
    detail = (cfg.get("_job_detail") or {})
    cancelled = detail.get("cancelled") or []

    if cancelled:
        for job in cancelled[:10]:
            line = f"  {job['job']}"
            if job.get("job_count"):
                line += f" (id {job['job_count']})"
            extra.append(line)
            extra.append(
                f"      user {job.get('user') or 'unknown'}"
                f" · started {job.get('started_at') or '—'}"
                f" · ended {job.get('ended_at') or '—'}"
                f" · ran {job.get('duration_text') or 'unknown'}")
            if job.get("verdict"):
                extra.append(f"      {job['verdict']}")
            if job.get("is_backup"):
                extra.append("      PRIORITY: this is a backup job. A missing "
                             "backup outranks every other item here.")
    else:
        item = _metric(metrics, "sap.sm37.cancelled_jobs")
        if item and item.detail:
            for name in [n.strip() for n in str(item.detail).split(",") if n.strip()][:8]:
                extra.append(f"  job: {name}")
            extra.append("  (names only -- per-job timing needs the TBTCO detail read)")

    for job in (detail.get("long_running") or [])[:5]:
        extra.append(f"  LONG RUNNING: {job['job']} · {job.get('duration_text')}"
                     f" · user {job.get('user') or 'unknown'}")

    return _evidence_for(metrics, ("sap.sm37.cancelled_jobs",), extra)


# --- dumps -----------------------------------------------------------------

def _dump_spike_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return _above(metrics, "sap.st22.dumps", float(t.get("dump_count", 5)))


def _dump_spike_evidence(metrics, events, cfg):
    extra = []
    item = _metric(metrics, "sap.st22.dumps")
    if item and item.detail:
        # The dump CLASS decides who owns it -- memory, database, ABAP or the
        # far side of an RFC. A count with no class is not actionable.
        extra.append(f"  dumps: {str(item.detail)[:400]}")
    extra.append("  Group by dump class in ST22 before attributing a cause")
    return _evidence_for(metrics, ("sap.st22.dumps", "memory", "cpu"), extra)


# --- interfaces ------------------------------------------------------------

def _interface_backlog_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return (
        _above(metrics, "sap.sm58.stuck_trfc", float(t.get("stuck_trfc", 5)))
        or _above(metrics, "sap.smq1.stuck_queues", float(t.get("stuck_queues", 1)) - 1)
        or _above(metrics, "sap.smq2.stuck_queues", float(t.get("stuck_queues", 1)) - 1)
        or _above(metrics, "sap.we02.failed_idocs", float(t.get("failed_idocs", 10)))
    )


def _interface_backlog_evidence(metrics, events, cfg):
    return _evidence_for(metrics, (
        "sap.sm58.stuck_trfc", "sap.smq1.stuck_queues",
        "sap.smq2.stuck_queues", "sap.we02.failed_idocs"),
        ["  SYSFAIL/CPICERR points at the DESTINATION, not this system -- "
         "test it in SM59 before changing anything here"])


# --- housekeeping ----------------------------------------------------------

def _housekeeping_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    if _above(metrics, "sap.sm21.errors", float(t.get("log_errors", 10))):
        return True
    for name, metric in metrics.items():
        if name.startswith("disk") and metric.value is not None:
            if metric.value >= float(t.get("disk_percent", 85)):
                return True
    return False


def _housekeeping_evidence(metrics, events, cfg):
    names = ["sap.sm21.errors"] + sorted(
        n for n in metrics if n.startswith("disk"))
    extra = []
    if not any(n.startswith("disk") for n in metrics):
        # Say so rather than letting a disk-free evidence list imply the disks
        # were checked and found fine.
        extra.append("  disk usage NOT read on this system "
                     "(needs SSH; RFC and SMON do not expose it)")
    return _evidence_for(metrics, names, extra)


# --- users / security ------------------------------------------------------

def _user_security_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return _above(metrics, "sap.su01.locked_users",
                  float(t.get("locked_users", 100)))


def _user_security_evidence(metrics, events, cfg):
    item = _metric(metrics, "sap.su01.locked_users")
    detail = cfg.get("_locked_user_detail") or {}

    if detail:
        # The split IS the diagnosis, so it leads.
        extra = [
            f"  {detail.get('admin_locked', 0)} locked by administrator "
            f"(housekeeping debt, not an incident)",
            f"  {detail.get('failed_logon_locked', 0)} locked by failed logons "
            f"(security signal)",
        ]
        if detail.get("verdict"):
            extra.append(f"  {detail['verdict']}")
        for account in (detail.get("service_accounts_locked") or [])[:8]:
            # A locked service account breaks an interface silently; a locked
            # dialog user phones the service desk.
            extra.append(f"  URGENT: {account['user']} is a {account['type']} "
                         f"account, {account['failed_attempts']} failed attempt(s)")
        for account in (detail.get("accounts") or [])[:8]:
            extra.append(f"  {account['user']} · {account['type']} · "
                         f"{account['failed_attempts']} failed attempt(s)")
    else:
        extra = [
            "  UFLAG 64 = locked by administrator (housekeeping debt)",
            "  UFLAG 128 = locked by failed logons (security signal)",
            "  The count alone cannot tell these apart -- split it in USR02 "
            "before treating this as either",
        ]
    if item and item.value is not None:
        crit = float((cfg.get("thresholds") or {}).get("locked_users_critical", 300))
        if item.value >= crit:
            extra.insert(0, f"  {int(item.value)} locked accounts is above the "
                            f"critical level of {int(crit)}")
    return _evidence_for(metrics, ("sap.su01.locked_users",), extra)


# --- backup ----------------------------------------------------------------

def _backup_gap_match(metrics, events, cfg):
    item = _metric(metrics, "sap.db12.last_backup")
    if item is None:
        return False
    # UNKNOWN must not fire this rule. SDBAH is empty on HANA and on ASE
    # without DB13, so "no row" means "not recorded here", not "no backup".
    # A false backup alarm at 3am costs credibility for every real one after.
    if item.status is Status.UNKNOWN:
        return False
    return item.status in (Status.WARNING, Status.CRITICAL)


def _backup_gap_evidence(metrics, events, cfg):
    return _evidence_for(metrics, ("sap.db12.last_backup",), [
        "  SDBAH only records backups run through the DBA Planning Calendar. "
        "Check the external backup tool before escalating.",
    ])


# --- buffers ---------------------------------------------------------------

def _buffer_swap_match(metrics, events, cfg):
    t = cfg.get("thresholds", {})
    return _above(metrics, "sap.st02.buffer_swaps",
                  float(t.get("buffer_swaps", 1)) - 1)


def _buffer_swap_evidence(metrics, events, cfg):
    return _evidence_for(metrics, ("sap.st02.buffer_swaps", "memory"), [
        "  Swaps accumulate since instance start -- a rising number between "
        "two readings is the signal, not the absolute value",
    ])


# --- reachability ----------------------------------------------------------
# This rule exists because of a real bug: an unreachable production system
# displayed 100/100 HEALTHY, because it produced zero cards and the scorer
# read zero problems as perfect health.

def _unreachable_match(metrics, events, cfg):
    item = _metric(metrics, "rfc.connection")
    if item is not None and item.status is Status.UNKNOWN:
        return True
    readable = [m for m in metrics.values() if m.value is not None]
    return bool(metrics) and not readable


def _unreachable_evidence(metrics, events, cfg):
    item = _metric(metrics, "rfc.connection")
    extra = []
    if item and item.detail:
        # The RFC error text distinguishes route denial from timeout from bad
        # credentials. Summarising it destroys the only diagnostic in it.
        extra.append(f"  RFC error: {str(item.detail)[:300]}")
    unknown = sum(1 for m in metrics.values() if m.status is Status.UNKNOWN)
    extra.append(f"  {unknown} of {len(metrics)} metrics unreadable")
    extra.append("  No culprit can be named until the system answers")
    return _evidence_for(metrics, ("rfc.connection",), extra)


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



# --- memory dump attribution -----------------------------------------------
# SAP_DUMP_SPIKE counts. This decides who owns a MEMORY-class spike, which is
# the one class where "the dump name is the diagnosis" is misleading: the
# same TSV_TNEW_PAGE_ALLOC_FAILED is ABAP when a Z report held the memory
# and Basis when nobody did. The ladder is in core/dump_attribution.py.

def _memory_dump_match(metrics, events, cfg):
    from core.dump_attribution import attribute
    return attribute(metrics, cfg) is not None


def _memory_dump_evidence(metrics, events, cfg):
    from core.dump_attribution import attribute
    v = attribute(metrics, cfg)
    if v is None:
        return []
    lines = [v.header(), f"  {v.reason}"]
    if v.culprit_users:
        lines.append(f"  users: {', '.join(v.culprit_users[:8])}")
    if v.collateral_programs:
        lines.append(f"  collateral (do not chase): {', '.join(v.collateral_programs[:8])}")
    lines.extend(f"  {e}" for e in v.evidence)
    return lines + _evidence_for(metrics, ("sap.sm50.priv_mode_wp", "sap.st03.top_user_memory_mb",
                                           "memory", "sap.sm66.max_instance_saturation_pct"))

_MATCHERS = {
    "SAP_LOCK_CONTENTION": (_lock_contention_match, _lock_contention_evidence),
    "INFRA_RESOURCE_PRESSURE": (_infra_pressure_match, _infra_pressure_evidence),
    "SAP_PROCESS_FAILURE": (_process_failure_match, _process_failure_evidence),
    "SAP_UPDATE_FAILURE": (_update_failure_match, _update_failure_evidence),
    "SAP_BATCH_FAILURE": (_batch_failure_match, _batch_failure_evidence),
    "SAP_DUMP_SPIKE": (_dump_spike_match, _dump_spike_evidence),
    "SAP_MEMORY_DUMP_ATTRIBUTION": (_memory_dump_match, _memory_dump_evidence),
    "SAP_INTERFACE_BACKLOG": (_interface_backlog_match, _interface_backlog_evidence),
    "SAP_HOUSEKEEPING_GAP": (_housekeeping_match, _housekeeping_evidence),
    "SAP_USER_SECURITY": (_user_security_match, _user_security_evidence),
    "SAP_BACKUP_GAP": (_backup_gap_match, _backup_gap_evidence),
    "SAP_BUFFER_SWAP": (_buffer_swap_match, _buffer_swap_evidence),
    "SAP_SYSTEM_UNREACHABLE": (_unreachable_match, _unreachable_evidence),
}


def unmatched_rules(path: str = CORRELATION_CONFIG_PATH) -> list[str]:
    """Rule IDs in the config that have no matcher and so can never fire.

    Surfaced on the Correlation page. A rule that loads, displays a threshold
    and is silently skipped by the engine is worse than a missing rule: it
    looks like coverage that does not exist.
    """
    return sorted(rid for rid in load_correlation_config(path)
                  if rid not in _MATCHERS)


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
    if rule_id == "SAP_MEMORY_DUMP_ATTRIBUTION":
        names = {"sap.st22.dump_count", "sap.st22.dumps", "sap.sm50.priv_mode_wp",
                 "sap.st03.top_user_memory_mb", "memory"}
        return sorted(n for n in names if n in metric_map)
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

    def correlate(self, metrics, events=None, now=None, detail=None) -> list[Incident]:
        """
        `detail` carries the per-item context a metric cannot hold -- which
        job failed and for how long, which accounts locked and why. Passed
        through the rule config so evidence builders can use it when it is
        available and fall back to counts when it is not, rather than the
        collectors and the rules having to agree on a schema.
        """
        now = now or datetime.now()
        metric_map = {metric.name: metric for metric in metrics}
        active_events = list(events) if events is not None else load_events(self.system)
        by_rule = {incident.rule_id: incident for incident in self.incidents}
        detail = detail or {}

        for rule in self.rules:
            config = dict(rule.config or {})
            config["_job_detail"] = detail.get("job_detail") or {}
            config["_locked_user_detail"] = detail.get("locked_user_detail") or {}

            matched = rule.matcher(metric_map, active_events, config)
            existing = by_rule.get(rule.rule_id)
            evidence = rule.evidence_builder(metric_map, active_events, config) if matched else []
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

"""
System health report: findings, recommended solutions, and a plain-language
summary.

DESIGN: DETERMINISTIC FIRST, LLM SECOND
---------------------------------------
Every finding here is produced by rules over collected metrics. The LLM is
optional and only ever *rewrites the prose summary*. It cannot invent a
finding, change a severity, or add a recommendation.

That ordering matters. A model asked to "analyse this system" will produce
confident-sounding root causes for readings it has no evidence about, and an
operator cannot tell the difference. Keeping severity deterministic means
the report is reproducible, explainable, and correct when the API is down.

If `USE_MOCK_AI=true` or no provider is configured, the report is still
complete -- it simply has a rule-written summary instead of a model-written
one.

WHAT A FINDING CONTAINS
-----------------------
    metric, tcode, severity, observation   -- what was measured
    why_it_matters                         -- operational consequence
    solutions                              -- concrete next actions
    evidence                               -- what supports it
    confidence                             -- and what is missing

UNKNOWN IS REPORTED, NOT HIDDEN
-------------------------------
Checks that could not be collected appear in their own section with the
reason. A report that silently omits them reads as "everything measured was
fine" when the truth is "we could not measure much".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from core.models import MonitoringResult, Status


@dataclass
class Finding:
    metric: str
    tcode: str
    severity: str
    observation: str
    why_it_matters: str
    solutions: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    confidence: str = "MEDIUM"

    # Filled from config/correlation_rules.yaml when a rule covers this
    # metric. Deterministic: the catalogue is a text file, not model output.
    culprit: dict = field(default_factory=dict)
    parameters: list[str] = field(default_factory=list)
    rule_id: str = ""
    trend: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "culprit": self.culprit, "parameters": self.parameters,
            "rule_id": self.rule_id, "trend": self.trend,
            "metric": self.metric, "tcode": self.tcode, "severity": self.severity,
            "observation": self.observation, "why_it_matters": self.why_it_matters,
            "solutions": self.solutions, "evidence": self.evidence,
            "missing_evidence": self.missing_evidence, "confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# Rule book: metric -> consequence and concrete actions
# ---------------------------------------------------------------------------
#
# Written as data so a Basis engineer can extend it without touching logic.
# "solutions" are investigation steps, not automated remediation -- nothing
# here changes a system.

RULES: dict[str, dict] = {
    "cpu": {
        "why": "Sustained high CPU lengthens dialog response for every user on this host.",
        "solutions": [
            "ST06 → check whether load is SAP work processes or another process on the host",
            "SM66 → identify long-running or PRIV-mode work processes",
            "SM50 → look for a single report consuming a work process for minutes",
            "If a background job is responsible, reschedule it outside business hours",
        ],
        "missing": ["Per-process CPU breakdown", "Historical baseline for this hour of day"],
    },
    "memory": {
        "why": "Memory pressure pushes the system into swap, which degrades every transaction.",
        "solutions": [
            "ST02 → check buffer swaps and whether buffers need resizing",
            "ST06 → confirm whether SAP or a non-SAP process holds the memory",
            "SM04 → look for sessions holding unusually large contexts",
        ],
        "missing": ["Swap activity trend", "Per-work-process memory use"],
    },
    "swap": {
        "why": "Active swapping means the host is over-committed; response times become unpredictable.",
        "solutions": [
            "ST06 → confirm swap-in/out rate rather than swap allocated",
            "Review abap/heap_area parameters against physical RAM",
            "Consider adding RAM or moving a workload off this host",
        ],
        "missing": ["Swap rate over time"],
    },
    "load_1m": {
        "why": "Run-queue depth above core count means processes are waiting for CPU.",
        "solutions": [
            "Compare the 1m, 5m and 15m averages to tell a spike from a trend",
            "SM66 → correlate with active work processes",
        ],
        "missing": ["Core count for this host"],
    },
    "sap.st22.dumps": {
        "why": "ABAP dumps mean transactions are failing outright; users see errors and data may be incomplete.",
        "solutions": [
            "ST22 → open each dump, capture the runtime error and terminating program",
            "Check whether one program or user accounts for all of them",
            "Correlate the dump time against recent transports (STMS)",
            "Search the SAP Note database for the runtime error text",
        ],
        "missing": ["Full ST22 call stack", "Exact termination statement", "Source line"],
    },
    "sap.sm12.lock_count": {
        "why": "Old locks block other users from the same objects, which looks like a hung system to them.",
        "solutions": [
            "SM12 → sort by age; a lock older than a few hours rarely belongs to live work",
            "Identify the holding user and confirm the session is still active in SM04",
            "Never delete a lock without confirming the owning transaction is dead",
        ],
        "missing": ["Lock age distribution", "Owning transaction per lock"],
    },
    "sap.sm13.failed_updates": {
        "why": "Failed V1 updates mean posted business documents were not written; this is data loss until resolved.",
        "solutions": [
            "SM13 → open each failed update and read the ABAP error",
            "Determine whether the update can be repeated or must be reversed",
            "Escalate to the functional owner of the affected document type",
        ],
        "missing": ["Update task error text", "Affected document numbers"],
    },
    "sap.sm37.cancelled_jobs": {
        "why": "Cancelled jobs mean scheduled business processing did not complete.",
        "solutions": [
            "SM37 → open the job log for the cancellation reason",
            "Check whether the same job has failed on previous days",
            "Confirm downstream dependencies were not silently skipped",
        ],
        "missing": ["Job log text", "Previous run history"],
    },
    "sap.sm58.stuck_entries": {
        "why": "Stuck tRFC entries mean interface data is not reaching the target system.",
        "solutions": [
            "SM58 → read the status text for the failing destination",
            "SM59 → test the RFC destination connection",
            "Confirm the target system is up and the user is not locked",
        ],
        "missing": ["Per-destination error text"],
    },
    "sap.smq1.entries": {
        "why": "Blocked outbound queues stall interface traffic and back up over time.",
        "solutions": ["SMQ1 → check the first entry's status; the queue is blocked behind it",
                      "Confirm the receiving system is reachable"],
        "missing": ["Queue error text"],
    },
    "sap.smq2.entries": {
        "why": "Blocked inbound queues stall incoming interface data.",
        "solutions": ["SMQ2 → check the first entry's status",
                      "Confirm the sending system is not re-sending duplicates"],
        "missing": ["Queue error text"],
    },
    "sap.sm21.errors": {
        "why": "System log errors often precede a visible outage.",
        "solutions": ["SM21 → read the error text and note the time window",
                      "Correlate with dumps, job failures and work-process state at the same time"],
        "missing": ["Full log detail"],
    },
    "sap.smlg.response_time": {
        "why": "Logon group response time is what users actually experience when they log on.",
        "solutions": [
            "SMLG → compare response time per instance; one slow instance skews the group",
            "ST03N → confirm whether dialog response is degraded system-wide or on one server",
            "SM66 → check for work-process saturation on the slow instance",
        ],
        "missing": ["Per-instance breakdown", "Historical baseline"],
    },
    "sap.sm66.wp_saturation_pct": {
        "why": "When dialog work processes saturate, new user requests queue and the system feels frozen.",
        "solutions": [
            "SM66 → identify what the busy processes are running",
            "Look for a single long-running report holding processes",
            "Consider raising rdisp/wp_no_dia if saturation is sustained, not spiky",
        ],
        "missing": ["Saturation duration", "Per-instance distribution"],
    },
    "sap.al08.user_logons": {
        "why": "An unusual session count can indicate runaway logons or a stuck interface user.",
        "solutions": ["AL08 → check for one user holding many sessions",
                      "SM04 → end orphaned sessions after confirming with the owner"],
        "missing": ["Session age", "Historical baseline"],
    },
}

# Disk metrics are per-mount (disk_/, disk_/usr/sap), matched by prefix.
_DISK_RULE = {
    "why": "A filesystem reaching capacity stops SAP writing logs, spool or work files, "
           "and a full /usr/sap can halt the instance.",
    "solutions": [
        "Identify the largest directories on the mount before deleting anything",
        "For /usr/sap, check old trace files and core dumps in the work directory",
        "Confirm log rotation and SAP housekeeping jobs are scheduled",
        "Extend the filesystem if growth is genuine rather than accumulated debris",
    ],
    "missing": ["Growth rate", "Largest directories on the mount"],
}


# ---------------------------------------------------------------------------
# Link metrics to the Basis incident catalogue
# ---------------------------------------------------------------------------
#
# RULES above gives a short "why it matters" per metric. The catalogue in
# config/correlation_rules.yaml carries the rest of what a consultant needs --
# which component is responsible, the SAP parameters that govern it, and the
# ordered remediation. Findings pull from the catalogue so that knowledge
# lives in ONE file: adding an incident type should not mean editing Python.
#
# A metric with no catalogue entry still produces a finding. It simply has no
# culprit attributed, which is honest -- naming one without a rule behind it
# would be a guess wearing the same styling as a fact.

_METRIC_TO_CATALOGUE: dict[str, str] = {
    "cpu": "INFRA_RESOURCE_PRESSURE",
    "memory": "INFRA_RESOURCE_PRESSURE",
    "memory.total_gb": "INFRA_RESOURCE_PRESSURE",
    "load_1m": "INFRA_RESOURCE_PRESSURE",
    "dialog_queue": "INFRA_RESOURCE_PRESSURE",
    "update_queue": "SAP_UPDATE_FAILURE",
    "db_rtt_ms": "INFRA_RESOURCE_PRESSURE",

    "sap.sm12.lock_count": "SAP_LOCK_CONTENTION",
    "sap.sm13.failed_updates": "SAP_UPDATE_FAILURE",
    "sap.sm37.cancelled_jobs": "SAP_BATCH_FAILURE",
    "sap.st22.dumps": "SAP_DUMP_SPIKE",
    "sap.sm58.stuck_trfc": "SAP_INTERFACE_BACKLOG",
    "sap.smq1.stuck_queues": "SAP_INTERFACE_BACKLOG",
    "sap.smq2.stuck_queues": "SAP_INTERFACE_BACKLOG",
    "sap.we02.failed_idocs": "SAP_INTERFACE_BACKLOG",
    "sap.su01.locked_users": "SAP_USER_SECURITY",
    "sap.db12.last_backup": "SAP_BACKUP_GAP",
    "sap.sm21.errors": "SAP_HOUSEKEEPING_GAP",
    "sap.sm50.wp_in_use": "SAP_PROCESS_FAILURE",
    "sap.dispatcher.state": "SAP_PROCESS_FAILURE",
    "sap.icm.state": "SAP_PROCESS_FAILURE",
    "sap.gateway.state": "SAP_PROCESS_FAILURE",
    "rfc.connection": "SAP_SYSTEM_UNREACHABLE",
}

_CATALOGUE_CACHE: dict | None = None


def _catalogue() -> dict:
    """The incident catalogue, loaded once. Never raises: a missing or broken
    rules file degrades findings, it does not break the report."""
    global _CATALOGUE_CACHE
    if _CATALOGUE_CACHE is None:
        try:
            from core.correlation import load_correlation_config
            _CATALOGUE_CACHE = load_correlation_config() or {}
        except Exception:
            _CATALOGUE_CACHE = {}
    return _CATALOGUE_CACHE


def _catalogue_for(metric_name: str) -> tuple[str, dict]:
    rule_id = _METRIC_TO_CATALOGUE.get(metric_name)
    if not rule_id and metric_name.startswith("disk"):
        rule_id = "SAP_HOUSEKEEPING_GAP"
    if not rule_id:
        return "", {}
    return rule_id, _catalogue().get(rule_id) or {}


def _rule_for(metric_name: str) -> dict | None:
    if metric_name in RULES:
        return RULES[metric_name]
    if metric_name.startswith("disk"):
        return _DISK_RULE
    return None


def _confidence(metric, rule) -> str:
    """
    Confidence reflects how much supporting evidence exists, not how strongly
    the number exceeds a threshold. A clear reading with no context is still
    a low-confidence explanation.
    """
    if metric.detail:
        return "HIGH" if len(rule.get("missing", [])) <= 1 else "MEDIUM"
    return "MEDIUM" if len(rule.get("missing", [])) <= 2 else "LOW"


def _trend_for(system: str, metric_name: str) -> dict:
    """
    Seven-day comparison for this metric, or {} when there is not enough
    history. A finding that says "memory 99%" is ambiguous; one that says
    "99%, up from 77% a week ago" is not, and the second is what decides
    whether anyone acts tonight.
    """
    try:
        from core.metric_history import trend as _trend
        data = _trend(system, metric_name, days=7)
        return data if data.get("available") else {}
    except Exception:
        return {}


def build_findings(result: MonitoringResult) -> list[Finding]:
    findings: list[Finding] = []

    for metric in result.metrics:
        if metric.status not in (Status.WARNING, Status.CRITICAL):
            continue
        rule = _rule_for(metric.name)
        cat_id, cat = _catalogue_for(metric.name)

        if rule is None:
            # No short interpretation rule. The catalogue may still cover this
            # metric, in which case the finding gets a real culprit and real
            # remediation rather than "review it manually".
            findings.append(Finding(
                metric=metric.name, tcode=metric.tcode or "—",
                severity=metric.status.value,
                observation=f"{metric.name} = {metric.display_value}",
                why_it_matters=(cat.get("description")
                                or "No interpretation rule is defined for this metric yet."),
                solutions=list(cat.get("remediation") or
                               [f"Review {metric.tcode or metric.name} manually"]),
                evidence=[f"Collected by {metric.source or 'unknown collector'}"]
                + ([f"Detail: {metric.detail[:220]}"] if metric.detail else []),
                missing_evidence=[] if cat else ["Operational rule for this metric"],
                confidence="MEDIUM" if cat else "LOW",
                culprit=dict(cat.get("culprit") or {}),
                parameters=list(cat.get("parameters") or []),
                rule_id=cat_id,
                trend=_trend_for(result.system, metric.name),
            ))
            continue

        evidence = [f"{metric.name} = {metric.display_value} "
                    f"(collected by {metric.source or 'unknown'})"]
        if metric.threshold_warning is not None:
            evidence.append(
                f"Threshold: warning {metric.threshold_warning:g}, "
                f"critical {metric.threshold_critical:g}"
                if metric.threshold_critical is not None else
                f"Warning threshold {metric.threshold_warning:g}")
        if metric.detail:
            evidence.append(f"Detail: {metric.detail[:220]}")

        tr = _trend_for(result.system, metric.name)
        if tr:
            direction = {"up": "up from", "down": "down from",
                         "flat": "unchanged from"}[tr["direction"]]
            line = (f"Trend: {tr['current']} — {direction} {tr['baseline']} "
                    f"{tr['days']} days ago "
                    f"({tr['change']:+g}, {tr['change_percent']:+g}%)")
            if not tr.get("confident"):
                # Name the thin part. Saying "only 24 samples" when 24 is the
                # whole series and 1 is the baseline points at the wrong
                # number and invites dismissing a real trend.
                line += (f" [baseline rests on {tr.get('baseline_samples', 0)} "
                         f"sample(s) — treat as indicative]")
            evidence.append(line)

        # Catalogue remediation wins where it exists: it is ordered, written
        # for someone acting under pressure, and maintained in one file. The
        # short RULES list stays as the fallback.
        solutions = list(cat.get("remediation") or rule["solutions"])

        findings.append(Finding(
            metric=metric.name, tcode=metric.tcode or "—",
            severity=metric.status.value,
            observation=f"{metric.name} = {metric.display_value}",
            why_it_matters=rule["why"],
            solutions=solutions,
            evidence=evidence,
            missing_evidence=list(rule.get("missing", [])),
            confidence=_confidence(metric, rule),
            culprit=dict(cat.get("culprit") or {}),
            parameters=list(cat.get("parameters") or []),
            rule_id=cat_id,
            trend=_trend_for(result.system, metric.name),
        ))

    order = {"CRITICAL": 0, "WARNING": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), f.metric))
    return findings


def _rule_summary(result: MonitoringResult, findings: list[Finding],
                  uncollected: list, ungraded: list = None) -> str:
    """Plain-language summary built from the data. No model involved."""
    crit = [f for f in findings if f.severity == "CRITICAL"]
    warn = [f for f in findings if f.severity == "WARNING"]
    # "Measured" counts everything that produced a value, graded or not --
    # an ungraded reading is still data we hold.
    measured = len(result.metrics) - len(uncollected)

    parts = [
        f"{result.system} (client {result.client}) is {result.overall_status.value} "
        f"as of {result.cycle_timestamp.strftime('%d %b %Y %H:%M')}. "
        f"{measured} of {len(result.metrics)} checks returned a reading."
    ]

    if crit:
        parts.append(
            "Critical: " + "; ".join(f"{f.tcode} {f.observation}" for f in crit[:3]) +
            f"{' and others' if len(crit) > 3 else ''}. "
            "These need attention before the next monitoring window.")
    if warn:
        parts.append(
            "Warning: " + "; ".join(f"{f.tcode} {f.observation}" for f in warn[:3]) +
            f"{' and others' if len(warn) > 3 else ''}.")
    if not crit and not warn:
        parts.append("No threshold breaches were detected in the checks that returned data.")

    if ungraded:
        parts.append(
            f"{len(ungraded)} check(s) returned a reading but have no threshold "
            f"defined, so they are reported without a verdict.")

    if uncollected:
        parts.append(
            f"{len(uncollected)} check(s) could not be collected "
            f"({', '.join(sorted({m.tcode or m.name for m in uncollected}))[:160]}). "
            "Their state is unknown rather than healthy.")

    return " ".join(parts)


def _llm_summary(prompt_context: str) -> str | None:
    """
    Optional. Rewrites the summary only; never produces findings.

    Uses this codebase's provider abstraction (evaluation/providers) rather
    than a private helper. An earlier version imported `_call_real_ai`, which
    exists in a different project -- the import always failed, so the LLM path
    silently never ran and every summary was rule-written regardless of
    configuration. Failing silently in the "optional enrichment" direction is
    easy to miss, which is exactly why it needed catching.

    Returns None whenever the model is unavailable or configured to mock, so
    the caller keeps the deterministic text.
    """
    if str(os.getenv("USE_MOCK_AI", "true")).strip().lower() in ("1", "true", "yes"):
        return None

    try:
        from evaluation.ai_analyzer import get_provider
        provider = get_provider()
    except Exception:
        return None

    if getattr(provider, "name", "") == "mock":
        return None

    instruction = (
        "You are writing the summary paragraph of a SAP Basis monitoring report "
        "for a technical manager. Use ONLY the facts below. Do not add causes, "
        "numbers or systems that are not present. Do not speculate about root "
        "cause. If evidence is missing, say so plainly. Three sentences maximum.\n\n"
        + prompt_context
    )

    try:
        text = provider.generate(instruction)
        return (text or "").strip() or None
    except Exception:
        # A model outage must never fail the report -- monitoring is
        # authoritative, AI is commentary.
        return None


def build_report(result: MonitoringResult, use_llm: bool = True) -> dict:
    """
    Returns the full report structure: summary, findings with solutions,
    uncollected checks, and a health score.
    """
    findings = build_findings(result)

    # UNKNOWN covers two very different situations and they must not share a
    # heading. "No value at all" is a blind spot. "Value present, but no
    # threshold defined" is a collected reading we simply do not grade -- the
    # SAP GUI collector emits many of these (sm37.active_jobs, sm51.
    # instances_started, sp01.spool_count). Listing them under "Not collected"
    # told the operator their SM37 was unmonitored when it had in fact been
    # read and screenshotted.
    def has_reading(metric) -> bool:
        text = str(metric.display_value or "").strip().lower()
        return bool(text) and text not in ("no data", "n/a", "-", "unknown", "none")

    uncollected = [m for m in result.metrics
                   if m.status is Status.UNKNOWN and not has_reading(m)]
    ungraded = [m for m in result.metrics
                if m.status is Status.UNKNOWN and has_reading(m)]

    # Score deducts for breaches AND for blind spots. A system we can barely
    # see should not score the same as one we can see and that is fine.
    score = 100
    for f in findings:
        score -= 12 if f.severity == "CRITICAL" else 5
    # Only true blind spots cost score. An ungraded reading is information
    # we have, just without a threshold -- that is a config gap, not a risk.
    score -= min(20, len(uncollected))
    score = max(0, score)

    summary = _rule_summary(result, findings, uncollected, ungraded)
    summary_source = "rules"

    if use_llm:
        context = (
            f"System: {result.system}, client {result.client}\n"
            f"Overall: {result.overall_status.value}\n"
            f"Findings:\n" +
            "\n".join(f"- [{f.severity}] {f.tcode} {f.observation} — {f.why_it_matters}"
                      for f in findings[:8]) +
            f"\nChecks with no data: {len(uncollected)}\n"
        )
        enriched = _llm_summary(context)
        if enriched:
            summary, summary_source = enriched, "llm"

    return {
        "system": result.system,
        "client": result.client,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cycle_timestamp": result.cycle_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "overall_status": result.overall_status.value,
        "health_score": score,
        "summary": summary,
        "summary_source": summary_source,
        "counts": {
            "critical": sum(1 for f in findings if f.severity == "CRITICAL"),
            "warning": sum(1 for f in findings if f.severity == "WARNING"),
            "uncollected": len(uncollected),
            "ungraded": len(ungraded),
            "measured": len(result.metrics) - len(uncollected),
        },
        "findings": [f.as_dict() for f in findings],
        "uncollected": [
            {"metric": m.name, "tcode": m.tcode or "—",
             "reason": m.detail or "collector returned no value"}
            for m in uncollected
        ],
        # Collected, but no threshold exists to grade them against. Add one in
        # the Configuration sheet of the Excel template to turn these into
        # graded checks.
        "ungraded": [
            {"metric": m.name, "tcode": m.tcode or "—",
             "value": m.display_value,
             "reason": m.detail or "no threshold defined for this metric"}
            for m in ungraded
        ],
        "errors": list(result.errors or []),
    }

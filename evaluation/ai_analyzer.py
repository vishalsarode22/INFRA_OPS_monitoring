"""Structured AI-assisted SAP Basis root-cause analysis with safety guardrails."""
from __future__ import annotations

import hashlib
import json
import os

from core.models import AIAnalysis, MonitoringResult
from evaluation.ai_context import build_rca_context, build_rca_prompt
from evaluation.ai_schemas import RCAAnalysis, parse_json_response
from evaluation.providers.base import AIProvider
from evaluation.providers.gemini import GeminiProvider
from evaluation.providers.mock import MockAIProvider
from utils.logger import get_logger

log = get_logger(__name__, "ai")

_SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "UNKNOWN": 2, "NORMAL": 3}


def get_provider() -> AIProvider:
    configured = os.getenv("AI_PROVIDER")
    if configured:
        provider = configured.strip().lower()
    else:
        use_mock = os.getenv("USE_MOCK_AI", "true").strip().lower() == "true"
        if use_mock:
            provider = "mock"
        elif os.getenv("AI_PROVIDER_CHAIN"):
            # A chain is configured, so use it rather than the single-key
            # path -- otherwise the extra keys sit unused until someone
            # notices AI_PROVIDER was never set.
            provider = "failover"
        else:
            provider = "gemini"

    if provider == "gemini":
        return GeminiProvider()
    if provider == "grok":
        from evaluation.providers.grok import GrokProvider
        return GrokProvider()
    if provider in ("failover", "chain", "multi"):
        from evaluation.providers.failover import FailoverProvider
        chain = FailoverProvider()
        log.info(f"AI provider chain: {chain.status()['configured']}")
        return chain
    if provider == "mock":
        return MockAIProvider()
    raise ValueError(f"Unsupported AI_PROVIDER: {provider}")


def _select_incident(result: MonitoringResult):
    active = [
        i for i in result.incidents
        if getattr(i.status, "value", i.status) != "RESOLVED"
    ]
    if not active:
        return None

    return sorted(
        active,
        key=lambda i: (
            _SEVERITY_ORDER.get(str(i.severity).upper(), 9),
            -float(i.confidence),
        ),
    )[0]


def _authoritative_severity(result: MonitoringResult, incident=None) -> str:
    """Return severity owned by deterministic monitoring, never by the LLM."""
    if incident is not None:
        return str(incident.severity).upper()
    return str(result.overall_status.value).upper()


def _enforce_authoritative_severity(
    rca: RCAAnalysis,
    authoritative: str,
) -> RCAAnalysis:
    """Prevent the LLM from downgrading/upgrading deterministic severity."""
    authoritative = authoritative.upper()
    if authoritative not in {"CRITICAL", "WARNING", "NORMAL", "UNKNOWN"}:
        authoritative = "UNKNOWN"

    if rca.severity != authoritative:
        original = rca.severity
        rca.severity = authoritative
        note = (
            f"LLM severity '{original}' was overridden by the deterministic "
            f"monitoring severity '{authoritative}'."
        )
        if note not in rca.limitations:
            rca.limitations.append(note)
    return rca.validate()


def _to_model(rca: RCAAnalysis) -> AIAnalysis:
    """
    Convert the provider-neutral RCA schema into the application's
    AIAnalysis model.

    Important:
    - Deterministic severity remains authoritative.
    - finding_status explicitly distinguishes a confirmed finding
      from an AI-generated hypothesis.
    - All structured RCA evidence is preserved.
    """

    return AIAnalysis(
        severity=rca.severity,
        likely_root_cause=rca.root_cause,
        evidence=list(rca.supporting_evidence),
        recommended_actions=list(rca.recommended_actions),
        confidence=rca.confidence,
        raw_response=rca.raw_response,

        root_cause_category=rca.root_cause_category,
        confidence_score=rca.confidence_score,

        supporting_metrics=list(rca.supporting_evidence),
        contradicting_evidence=list(rca.contradicting_evidence),
        limitations=list(rca.limitations),

        # RCA classification:
        #
        # CONFIRMED:
        #   Supplied evidence actually proves the finding.
        #
        # HYPOTHESIS:
        #   AI inference that requires further verification.
        #
        # UNKNOWN:
        #   Evidence is insufficient to form a useful finding.
        #
        # Never allow a missing provider field to accidentally
        # become a confirmed finding.
        finding_status=getattr(
            rca,
            "finding_status",
            "HYPOTHESIS",
        ),
    )


def analyze(
    result: MonitoringResult,
    provider: AIProvider | None = None,
    incident=None,
    intelligence=None,
    gui_results=None,
    attribution=None,
) -> AIAnalysis:
    """Analyze the most important available monitoring evidence.

When an incident is supplied, the incident is analyzed.
When no incident is supplied, the complete monitoring result
and GUI evidence are analyzed.
"""
    incident = incident or _select_incident(result)
    authoritative = _authoritative_severity(result, incident)
    context = build_rca_context(
    result,
    incident=incident,
    gui_results=gui_results,
    )

    if intelligence is not None:
        from core.ai_intelligence_context import build_ai_intelligence_context

        ai_intelligence = build_ai_intelligence_context(intelligence)
        context["operational_intelligence"] = ai_intelligence.as_dict()

    if attribution:
        # Named things behind the counters: who dumped, which job failed and
        # why, which report holds a work process, who drives response time.
        # Computed deterministically; the model explains, it does not invent.
        context["attribution"] = attribution

    context["ai_contract"] = {
        "authoritative_severity": authoritative,
        "severity_source": "deterministic_monitoring",
        "llm_role": "explain_evidence_and_recommend_investigation_steps",
    }
    prompt = build_rca_prompt(context)
    provider = provider or get_provider()

    try:
        raw = provider.generate(prompt)
        rca = parse_json_response(raw)
        rca = _enforce_authoritative_severity(rca, authoritative)
        return _to_model(rca)
    except Exception as exc:
        log.warning("Structured AI analysis failed: %s", exc)

        # Preserve the raw provider response for debugging malformed
        # structured output. Never expose credentials or secrets.
        if "raw" in locals() and raw:
            try:
                debug_path = os.path.join(
                    "logs",
                    "ai_invalid_response.txt",
                )

                os.makedirs("logs", exist_ok=True)

                with open(
                    debug_path,
                    "w",
                    encoding="utf-8",
                ) as fh:
                    fh.write(str(raw))

                log.warning(
                    "Invalid AI response saved to %s",
                    debug_path,
                )
            except Exception as debug_exc:
                log.warning(
                    "Could not save invalid AI response: %s",
                    type(debug_exc).__name__,
                )

        fallback = RCAAnalysis(
            severity=authoritative,
            root_cause_category="AI_UNAVAILABLE",
            root_cause=(
                "The deterministic monitoring and correlation engines completed, "
                "but structured LLM analysis was unavailable."
            ),
            confidence="LOW",
            confidence_score=0.0,
            supporting_evidence=(
                incident.evidence[:10]
                if incident
                else [
                    f"{m.name}={m.display_value} ({m.status.value})"
                    for m in result.metrics
                    if m.status.value in {"CRITICAL", "WARNING"}
                ][:10]
            ),
            recommended_actions=[
                "Review the active incident and its evidence manually.",
                "Retry AI analysis after provider connectivity is restored.",
            ],
            limitations=[str(exc)[:500]],
            raw_response="",
        ).validate()
        return _to_model(fallback)



def _incident_ai_fingerprint(incident) -> str:
    """Stable fingerprint for evidence that should trigger AI re-analysis."""
    payload = {
        "rule_id": getattr(incident, "rule_id", ""),
        "severity": str(getattr(incident, "severity", "")),
        "evidence": list(getattr(incident, "evidence", []) or []),
        "affected_metrics": list(
            getattr(incident, "affected_metrics", []) or []
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def _persist_ai_analysis(
    incident,
    analysis: AIAnalysis,
    fingerprint: str,
) -> None:
    """
    Persist structured AI RCA onto the incident.

    AI findings are advisory. The deterministic incident severity remains
    authoritative and must never be replaced by the LLM output.
    """

    payload = {
        # Deterministic/authoritative severity is retained for reporting.
        "severity": analysis.severity,

        # RCA classification.
        "root_cause_category": analysis.root_cause_category,
        "likely_root_cause": analysis.likely_root_cause,

        # Explicit distinction between confirmed and inferred findings.
        "finding_status": getattr(
            analysis,
            "finding_status",
            "HYPOTHESIS",
        ),

        # AI confidence.
        "confidence": analysis.confidence,
        "confidence_score": analysis.confidence_score,

        # Evidence.
        "evidence": list(
            getattr(analysis, "evidence", []) or []
        ),
        "supporting_metrics": list(
            getattr(analysis, "supporting_metrics", []) or []
        ),
        "contradicting_evidence": list(
            getattr(analysis, "contradicting_evidence", []) or []
        ),

        # Investigation guidance.
        "recommended_actions": list(
            getattr(analysis, "recommended_actions", []) or []
        ),

        # AI limitations / uncertainty.
        "limitations": list(
            getattr(analysis, "limitations", []) or []
        ),

        # Preserve the original provider response for audit/debugging.
        "raw_response": getattr(
            analysis,
            "raw_response",
            "",
        ),
    }

    # The incident model introduced these fields in the persistence
    # milestone. getattr/setattr keeps this helper compatible with
    # older incident objects.
    setattr(
        incident,
        "ai_analysis",
        payload,
    )

    setattr(
        incident,
        "ai_analysis_fingerprint",
        fingerprint,
    )

    from datetime import datetime

    setattr(
        incident,
        "ai_analysis_at",
        datetime.now(),
    )

    setattr(
        incident,
        "ai_reanalysis_required",
        False,
    )


def analyze_incidents(
    result: MonitoringResult,
    provider: AIProvider | None = None,
    intelligence=None,
) -> list[AIAnalysis]:
    """
    Analyze active incidents and ST22 dump evidence.

    Existing incident AI behavior is preserved.

    Additionally, ST22 is analyzed when one or more dumps were
    collected, even if the correlation engine did not create an
    incident for the ST22 metric.

    Deterministic monitoring remains authoritative for severity.
    """

    analyses: list[AIAnalysis] = []

    gui_results = getattr(
        result,
        "gui_results",
        None,
    ) or []

    # ---------------------------------------------------------
    # Existing incident-based AI analysis
    # ---------------------------------------------------------

    for incident in list(
        getattr(result, "incidents", []) or []
    ):
        status = getattr(
            getattr(incident, "status", None),
            "value",
            getattr(incident, "status", ""),
        )

        if str(status).upper() == "RESOLVED":
            continue

        fingerprint = _incident_ai_fingerprint(
            incident
        )

        existing_fingerprint = getattr(
            incident,
            "ai_analysis_fingerprint",
            None,
        )

        needs_reanalysis = bool(
            getattr(
                incident,
                "ai_reanalysis_required",
                False,
            )
        )

        if (
            existing_fingerprint
            and existing_fingerprint == fingerprint
            and not needs_reanalysis
        ):
            continue

        analysis = analyze(
            result,
            provider=provider,
            incident=incident,
            intelligence=intelligence,
            gui_results=gui_results,
        )

        _persist_ai_analysis(
            incident,
            analysis,
            fingerprint,
        )

        analyses.append(analysis)

    # ---------------------------------------------------------
    # ST22-specific AI analysis
    #
    # ST22 must be investigated whenever dumps exist,
    # even when no incident was generated.
    # ---------------------------------------------------------

    st22_evidence = []

    for gui_result in gui_results:

        tcode = str(
            getattr(
                gui_result,
                "tcode",
                "",
            )
            or ""
        ).upper()

        if tcode != "ST22":
            continue

        extra_data = getattr(
            gui_result,
            "extra_data",
            None,
        )

        if not isinstance(extra_data, dict):
            continue

        dump_count = extra_data.get(
            "dump_count",
            0,
        )

        try:
            dump_count = int(
                float(dump_count or 0)
            )
        except (
            TypeError,
            ValueError,
        ):
            dump_count = 0

        if dump_count > 0:
            st22_evidence.append(
                gui_result
            )

    # No dumps means no ST22 AI analysis.
    if st22_evidence:

        log.info(
            "ST22 AI analysis triggered: %d evidence record(s)",
            len(st22_evidence),
        )

        # ST22 severity is already represented by the
        # deterministic monitoring result.
        #
        # We deliberately do not create a fake Incident object.
        # analyze() can operate without one and will use
        # result.overall_status as authoritative severity.

        st22_analysis = analyze(
            result,
            provider=provider,
            incident=None,
            intelligence=intelligence,
            gui_results=st22_evidence,
        )

        analyses.append(
            st22_analysis
        )

    return analyses
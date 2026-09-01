"""Controlled live LLM RCA smoke test for InfraBeatOps.

Run from the project root with:
    python scripts\\live_ai_rca_test.py

This uses synthetic SAP data only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Make project-root imports work when executing scripts\live_ai_rca_test.py.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.incidents import Incident, IncidentStatus
from core.models import MetricResult, MonitoringResult, Status
from evaluation.ai_analyzer import analyze


def build_synthetic_incident() -> MonitoringResult:
    now = datetime.now()
    result = MonitoringResult(system="SYNTHETIC-PRD", client="000")

    result.metrics = [
        MetricResult(
            name="sap.sm12.lock_count",
            value=23,
            display_value="23",
            status=Status.WARNING,
            source="SYNTHETIC",
            tcode="SM12",
            detail="Synthetic lock count",
        ),
        MetricResult(
            name="sap.st03n.dialog_response_time",
            value=780,
            display_value="780 ms",
            status=Status.WARNING,
            source="SYNTHETIC",
            tcode="ST03N",
            detail="Synthetic response time",
        ),
        MetricResult(
            name="sap.st22.dump_count",
            value=4,
            display_value="4",
            status=Status.WARNING,
            source="SYNTHETIC",
            tcode="ST22",
            detail="Synthetic ABAP dump count",
        ),
        MetricResult(
            name="cpu",
            value=18,
            display_value="18%",
            status=Status.NORMAL,
            source="SYNTHETIC",
            detail="Synthetic CPU",
        ),
    ]
    result.overall_status = Status.WARNING

    result.incidents = [
        Incident(
            incident_id="INC-SYNTHETIC-001",
            system=result.system,
            client=result.client,
            rule_id="SAP_LOCK_CONTENTION",
            title="Synthetic SAP lock contention",
            severity="WARNING",
            status=IncidentStatus.ACTIVE,
            first_seen=now,
            last_seen=now,
            affected_metrics=[
                "sap.sm12.lock_count",
                "sap.st03n.dialog_response_time",
                "sap.st22.dump_count",
            ],
            evidence=[
                "SM12 lock count = 23",
                "ST03N dialog response = 780 ms",
                "ST22 dump count = 4",
                "CPU = 18%",
            ],
            confidence=0.90,
            description="Synthetic incident for provider smoke testing.",
        )
    ]
    return result


def main() -> int:
    if os.getenv("USE_MOCK_AI", "true").strip().lower() == "true":
        print("ERROR: USE_MOCK_AI is true. Set it to false for the live test.")
        return 2

    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider == "mock":
        print("ERROR: AI_PROVIDER=mock. Select a real provider.")
        return 2

    result = build_synthetic_incident()
    analysis = analyze(result)

    print(json.dumps({
        "provider": provider,
        "severity": analysis.severity,
        "root_cause_category": analysis.root_cause_category,
        "likely_root_cause": analysis.likely_root_cause,
        "confidence": analysis.confidence,
        "confidence_score": analysis.confidence_score,
        "evidence": analysis.evidence,
        "recommended_actions": analysis.recommended_actions,
        "limitations": analysis.limitations,
    }, indent=2))

    if not analysis.likely_root_cause:
        print("ERROR: Provider returned no usable RCA.")
        return 1

    print("\nLive LLM RCA smoke test completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

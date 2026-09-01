"""Validated, provider-neutral schema for AI root-cause analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import json
import re


ALLOWED_SEVERITIES = {
    "CRITICAL",
    "WARNING",
    "NORMAL",
    "UNKNOWN",
}

ALLOWED_CONFIDENCE = {
    "HIGH",
    "MEDIUM",
    "LOW",
}

ALLOWED_FINDING_STATUS = {
    "OBSERVED",
    "HYPOTHESIS",
    "CORRELATED",
    "CONFIRMED",
}


@dataclass
class RCAAnalysis:
    """Structured RCA result accepted from an LLM provider."""

    severity: str = "UNKNOWN"
    root_cause_category: str = "UNKNOWN"
    finding_status: str = "HYPOTHESIS"
    root_cause: str = ""
    confidence: str = "LOW"
    confidence_score: float | None = None
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    raw_response: str = ""

    def validate(self) -> "RCAAnalysis":
        """Normalize and validate the structured RCA result."""

        # --------------------------------------------------------------
        # Severity
        # --------------------------------------------------------------

        self.severity = str(
            self.severity or "UNKNOWN"
        ).upper()

        if self.severity not in ALLOWED_SEVERITIES:
            self.severity = "UNKNOWN"

        # --------------------------------------------------------------
        # Confidence
        # --------------------------------------------------------------

        self.confidence = str(
            self.confidence or "LOW"
        ).upper()

        if self.confidence not in ALLOWED_CONFIDENCE:
            self.confidence = "LOW"

        # --------------------------------------------------------------
        # Finding status
        #
        # OBSERVED    = evidence proves the event/finding occurred
        # HYPOTHESIS  = plausible inferred cause
        # CORRELATED  = multiple independent signals support pattern
        # CONFIRMED   = supplied evidence establishes the cause
        # --------------------------------------------------------------

        self.finding_status = str(
            self.finding_status or "HYPOTHESIS"
        ).upper()

        if self.finding_status not in ALLOWED_FINDING_STATUS:
            self.finding_status = "HYPOTHESIS"

        # --------------------------------------------------------------
        # Root cause
        # --------------------------------------------------------------

        self.root_cause_category = str(
            self.root_cause_category or "UNKNOWN"
        ).strip()[:120]

        self.root_cause = str(
            self.root_cause or ""
        ).strip()[:2000]

        # --------------------------------------------------------------
        # Confidence score
        # --------------------------------------------------------------

        if self.confidence_score is not None:
            try:
                self.confidence_score = max(
                    0.0,
                    min(
                        1.0,
                        float(self.confidence_score),
                    ),
                )
            except (
                TypeError,
                ValueError,
            ):
                self.confidence_score = None

        # --------------------------------------------------------------
        # Lists
        # --------------------------------------------------------------

        self.supporting_evidence = _clean_list(
            self.supporting_evidence,
            20,
            500,
        )

        self.contradicting_evidence = _clean_list(
            self.contradicting_evidence,
            20,
            500,
        )

        self.recommended_actions = _clean_list(
            self.recommended_actions,
            15,
            500,
        )

        self.limitations = _clean_list(
            self.limitations,
            10,
            500,
        )

        return self

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        raw_response: str = "",
    ) -> "RCAAnalysis":
        """Create and validate an RCA object from provider JSON."""

        if not isinstance(data, dict):
            raise ValueError(
                "RCA response must be a JSON object."
            )

        # --------------------------------------------------------------
        # Root cause can be either:
        #
        # {
        #   "root_cause": {
        #       "category": "...",
        #       "description": "..."
        #   }
        # }
        #
        # or:
        #
        # {
        #   "root_cause": "..."
        # }
        # --------------------------------------------------------------

        root = data.get(
            "root_cause"
        ) or {}

        if isinstance(root, str):

            root_cause = root

            category = data.get(
                "root_cause_category",
                "UNKNOWN",
            )

            score = data.get(
                "confidence_score"
            )

        elif isinstance(root, dict):

            root_cause = root.get(
                "description",
                root.get(
                    "summary",
                    "",
                ),
            )

            category = root.get(
                "category",
                data.get(
                    "root_cause_category",
                    "UNKNOWN",
                ),
            )

            score = root.get(
                "confidence_score",
                data.get(
                    "confidence_score"
                ),
            )

        else:

            root_cause = ""
            category = data.get(
                "root_cause_category",
                "UNKNOWN",
            )
            score = data.get(
                "confidence_score"
            )

        # --------------------------------------------------------------
        # Build RCA object
        # --------------------------------------------------------------

        result = cls(
            severity=data.get(
                "severity",
                "UNKNOWN",
            ),

            root_cause_category=category,

            finding_status=data.get(
                "finding_status",
                "HYPOTHESIS",
            ),

            root_cause=root_cause,

            confidence=data.get(
                "confidence",
                "LOW",
            ),

            confidence_score=score,

            supporting_evidence=data.get(
                "supporting_evidence",
                data.get(
                    "evidence",
                    [],
                ),
            ),

            contradicting_evidence=data.get(
                "contradicting_evidence",
                [],
            ),

            recommended_actions=data.get(
                "recommended_actions",
                [],
            ),

            limitations=data.get(
                "limitations",
                [],
            ),

            raw_response=raw_response,
        )

        return result.validate()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the validated RCA result."""

        return {
            "severity": self.severity,

            "root_cause": {
                "category": self.root_cause_category,
                "description": self.root_cause,
            },

            "finding_status": self.finding_status,

            "confidence": self.confidence,

            "confidence_score": self.confidence_score,

            "supporting_evidence": self.supporting_evidence,

            "contradicting_evidence": self.contradicting_evidence,

            "recommended_actions": self.recommended_actions,

            "limitations": self.limitations,
        }


def parse_json_response(
    raw_response: str,
) -> RCAAnalysis:
    """Parse strict JSON or JSON enclosed in a markdown fence."""

    text = (
        raw_response or ""
    ).strip()

    if not text:
        raise ValueError(
            "Empty AI response."
        )

    # --------------------------------------------------------------
    # Remove optional Markdown JSON fence
    # --------------------------------------------------------------

    if text.startswith("```"):

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

    # --------------------------------------------------------------
    # Strict JSON parsing
    # --------------------------------------------------------------

    try:

        data = json.loads(
            text
        )

    except json.JSONDecodeError as exc:

        raise ValueError(
            f"AI response was not valid JSON: {exc}"
        ) from exc

    return RCAAnalysis.from_dict(
        data,
        raw_response=raw_response,
    )


def _clean_list(
    value: Any,
    maximum: int,
    item_length: int,
) -> list[str]:
    """Normalize a provider list into bounded strings."""

    if value is None:
        return []

    if isinstance(value, str):
        value = [value]

    if not isinstance(
        value,
        (list, tuple),
    ):
        return []

    result = []

    for item in value[:maximum]:

        item = str(
            item
        ).strip()

        if item:

            result.append(
                item[:item_length]
            )

    return result
"""Safe adapter for passing deterministic intelligence to the RCA layer.

This module deliberately does not call an LLM and does not alter incident
severity. It converts the fusion object into a bounded, structured context
block that an existing AI provider can consume.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from core.intelligence_fusion import OperationalIntelligence


@dataclass(frozen=True)
class AIIntelligenceContext:
    overall_signal: str
    score: float
    findings: tuple[str, ...]
    limitations: tuple[str, ...]
    instruction: str

    def as_dict(self) -> dict:
        return {
            "overall_signal": self.overall_signal,
            "score": self.score,
            "findings": list(self.findings),
            "limitations": list(self.limitations),
            "instruction": self.instruction,
        }


def _clean(text: str, limit: int = 400) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def build_ai_intelligence_context(
    intelligence: OperationalIntelligence,
    *,
    max_findings: int = 5,
    max_limitations: int = 5,
) -> AIIntelligenceContext:
    """Build bounded RCA context from deterministic intelligence.

    The LLM is explicitly instructed to treat this as evidence, not as an
    authoritative severity or confirmed root cause.
    """
    if max_findings < 0 or max_limitations < 0:
        raise ValueError("context limits cannot be negative")

    findings = tuple(
        _clean(x) for x in intelligence.key_findings[:max_findings]
    )
    limitations = tuple(
        _clean(x) for x in intelligence.limitations[:max_limitations]
    )

    instruction = (
        "Treat operational intelligence as supporting evidence only. "
        "Do not change the authoritative incident severity. "
        "Do not present an inferred cause as a confirmed root cause. "
        "Explain which evidence supports each hypothesis and state "
        "uncertainty when evidence is incomplete."
    )

    return AIIntelligenceContext(
        overall_signal=_clean(intelligence.overall_signal, 50),
        score=max(0.0, min(1.0, float(intelligence.score))),
        findings=findings,
        limitations=limitations,
        instruction=instruction,
    )


def render_ai_intelligence_context(
    context: AIIntelligenceContext,
) -> str:
    """Render a stable, prompt-safe text block for an RCA provider."""
    lines = [
        "OPERATIONAL INTELLIGENCE",
        f"Signal: {context.overall_signal}",
        f"Score: {context.score:.4f}",
        "Findings:",
    ]
    lines.extend(f"- {item}" for item in context.findings)
    if context.limitations:
        lines.append("Limitations:")
        lines.extend(f"- {item}" for item in context.limitations)
    lines.append(f"Guardrail: {context.instruction}")
    return "\n".join(lines)

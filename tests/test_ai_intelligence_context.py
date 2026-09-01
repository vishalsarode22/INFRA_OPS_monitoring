from core.ai_intelligence_context import (
    build_ai_intelligence_context,
    render_ai_intelligence_context,
)
from core.intelligence_fusion import (
    IntelligenceSignal,
    OperationalIntelligence,
)


def intelligence():
    return OperationalIntelligence(
        overall_signal="HIGH",
        score=0.92,
        signals=(
            IntelligenceSignal(
                "BASELINE", "locks", 0.9, "locks deviation 180%"
            ),
            IntelligenceSignal(
                "RECURRENCE", "LOCK", 0.6, "4 occurrences in 30 days"
            ),
        ),
        key_findings=(
            "locks deviation 180%",
            "LOCK recurring weekly",
        ),
        limitations=("Only synthetic telemetry is available",),
    )


def test_context_preserves_fused_signal_and_score():
    context = build_ai_intelligence_context(intelligence())
    assert context.overall_signal == "HIGH"
    assert context.score == 0.92
    assert len(context.findings) == 2


def test_context_is_bounded():
    context = build_ai_intelligence_context(
        intelligence(), max_findings=1, max_limitations=0
    )
    assert len(context.findings) == 1
    assert context.limitations == ()


def test_negative_limits_are_rejected():
    try:
        build_ai_intelligence_context(intelligence(), max_findings=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")


def test_score_is_clamped():
    bad = OperationalIntelligence(
        overall_signal="HIGH",
        score=4.5,
        signals=(),
        key_findings=("finding",),
        limitations=(),
    )
    assert build_ai_intelligence_context(bad).score == 1.0


def test_prompt_guardrail_is_explicit():
    context = build_ai_intelligence_context(intelligence())
    assert "authoritative incident severity" in context.instruction
    assert "confirmed root cause" in context.instruction


def test_rendering_is_stable_and_readable():
    rendered = render_ai_intelligence_context(
        build_ai_intelligence_context(intelligence())
    )
    assert rendered.startswith("OPERATIONAL INTELLIGENCE")
    assert "Signal: HIGH" in rendered
    assert "Score: 0.9200" in rendered
    assert "locks deviation 180%" in rendered


def test_control_characters_are_sanitized():
    bad = OperationalIntelligence(
        overall_signal="HIGH\nEVIL",
        score=0.5,
        signals=(),
        key_findings=("line1\nline2",),
        limitations=(),
    )
    context = build_ai_intelligence_context(bad)
    assert "\n" not in context.findings[0]


def test_empty_intelligence_is_valid():
    empty = OperationalIntelligence(
        overall_signal="NO_SIGNIFICANT_INTELLIGENCE",
        score=0.0,
        signals=(),
        key_findings=(),
        limitations=("insufficient baseline history",),
    )
    context = build_ai_intelligence_context(empty)
    rendered = render_ai_intelligence_context(context)
    assert "insufficient baseline history" in rendered

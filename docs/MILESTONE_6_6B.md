# Milestone 6.6b — Intelligence → Existing RCA Integration

The deterministic OperationalIntelligence object can now be passed to the
existing `evaluation.ai_analyzer.analyze()` function through an optional
`intelligence=` argument.

Backwards compatibility:
- Existing callers do not need to change.
- If intelligence is omitted, the RCA flow behaves as before.
- No LLM/provider implementation is changed.

Safety:
- deterministic incident severity remains authoritative
- operational intelligence is supporting evidence
- the LLM cannot convert an inferred cause into a confirmed cause
- the existing prompt guardrails remain active

This is the first point where baseline, pre-incident change and recurrence
intelligence can reach Gemini through the existing RCA path.

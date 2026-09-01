# Milestone 6.6 — Intelligence → AI RCA Context

This milestone creates a safe adapter between deterministic operational
intelligence and the existing RCA/LLM layer.

It does NOT call an LLM and does NOT modify the existing AI provider.
Instead it provides a bounded, structured context object and a stable text
rendering that can be inserted into the existing incident-focused prompt.

Guardrails:
- deterministic incident severity remains authoritative
- intelligence is evidence, not a confirmed root cause
- findings and limitations are bounded
- control characters are sanitized
- score is constrained to [0, 1]

Integration with the existing provider should be the next sub-step after
this adapter is green.

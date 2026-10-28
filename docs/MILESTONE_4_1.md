# Milestone 4.1 — Structured AI/RCA foundation

Adds a provider-neutral LLM layer without making external API calls during tests.

## Flow

MonitoringResult -> RCA context builder -> sanitization -> provider -> JSON validation -> AIAnalysis

## Providers

- `mock`: deterministic offline provider (default)
- `gemini`: existing Gemini integration, now isolated behind `AIProvider`

Set `AI_PROVIDER=gemini` and `GEMINI_API_KEY` only when you are ready to make a real call.

## Safety

The deterministic monitoring and correlation engines remain authoritative. LLM output is explanatory and advisory. Invalid provider output falls back to a low-confidence `AI_UNAVAILABLE` analysis.

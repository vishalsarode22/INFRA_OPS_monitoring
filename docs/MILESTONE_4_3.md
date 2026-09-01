# InfraBeatOps Milestone 4.3 — LLM Safety Guardrails and Live Smoke Test

## Purpose

Connect the structured RCA engine to a real provider safely, without allowing
the LLM to override deterministic monitoring severity.

## Changes

- Deterministic severity is authoritative.
- Evidence is explicitly treated as untrusted data in the prompt.
- LLM prompt injection through monitoring text is explicitly rejected.
- Offline guardrail tests were added.
- A controlled synthetic live-provider smoke test was added.
- No production credentials are included in this patch.

## Offline validation

```bash
python -m pytest tests -v
```

## Controlled live test

Keep real credentials out of source files. Configure the provider through
environment variables or your existing secret mechanism.

Example on Windows CMD:

```bat
set USE_MOCK_AI=false
set AI_PROVIDER=gemini
set GEMINI_API_KEY=<your-key>
python scripts/live_ai_rca_test.py
```

The script uses synthetic SAP data only and does not print the API key.

## Safety rule

The LLM explains and recommends. It does not decide the authoritative
monitoring severity, perform remediation, or receive credentials.

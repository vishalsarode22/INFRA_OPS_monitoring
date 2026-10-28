# Milestone 6.6b Fix — Preserve AI Incident Persistence

The first 6.6b patch replaced the existing `evaluation.ai_analyzer` module
too aggressively and dropped the previously established `analyze_incidents`
compatibility API.

This fix restores that orchestration API while retaining the new optional
`intelligence=` parameter.

Behavior:
- New incidents receive AI analysis.
- Existing unchanged incidents are skipped.
- Changed evidence / explicit reanalysis triggers a new provider call.
- AI analysis is persisted on the incident.
- Resolved incidents are skipped.
- Existing deterministic severity guardrails remain active.
- Existing callers of `analyze()` remain compatible.

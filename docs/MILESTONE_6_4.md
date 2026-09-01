# Milestone 6.4 — Incident Recurrence & Pattern Intelligence

Adds deterministic recurrence intelligence over resolved and active
incident occurrences.

It answers:
- Is this rule recurring?
- How frequently does it recur?
- Is it a frequent or weekly pattern?
- How many occurrences had verified resolutions?
- Is the same confirmed root cause repeating?

Recurrence is advisory and does not modify incident severity.

This is deliberately deterministic before introducing machine learning.
Later, recurrence features can become inputs to forecasting and anomaly
models.

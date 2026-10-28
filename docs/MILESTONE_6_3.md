# Milestone 6.3 — Pre-Incident Change Detection

Adds an explainable comparison between an earlier and recent portion of a
lookback window.

It answers:
- What changed before an incident?
- In which direction?
- By approximately how much?
- Was the change material?

The detector does not change incident severity. It produces advisory
evidence that can later be attached to an incident and passed to RCA.

Example:
  SM12 locks: prior mean 5.7 -> recent mean 22.0
  change: +285.8%
  significance: MATERIAL

This is intentionally not ML. It creates clean, auditable features that
can later become inputs to anomaly/forecasting models after real telemetry
has been collected.

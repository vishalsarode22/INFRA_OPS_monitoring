# Milestone 6.1 — Baseline & Trend Engine

Adds deterministic historical interpretation without introducing an ML
dependency.

The engine calculates:
- mean, median, min, max
- population standard deviation
- p95
- trend direction
- rate of change
- percentage deviation from baseline
- z-score
- anomaly-candidate assessment

It deliberately requires a minimum amount of history before making anomaly
claims. Missing, invalid, and non-finite samples are ignored rather than
converted into false health signals.

This layer is advisory to the incident engine. It should not replace SAP
thresholds or deterministic severity until it has been validated against
real historical telemetry.

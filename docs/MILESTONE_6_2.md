# Milestone 6.2 — Baseline Pipeline Integration

Adds a safe integration boundary between monitoring results and baseline
intelligence.

## Safety contract

Baseline intelligence is advisory. It never mutates MetricResult.status and
therefore cannot directly downgrade or upgrade deterministic SAP severity.

The current sample is evaluated against prior history before it is appended,
preventing self-contamination of the baseline.

UNKNOWN/non-numeric values are ignored. History is bounded in memory.

## Why this boundary

The next milestone can attach the returned MetricIntelligence to the existing
monitoring result/event context without rewriting the incident engine. After
real telemetry validation, persistence can be introduced for long-lived
baselines.

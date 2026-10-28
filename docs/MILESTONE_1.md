# InfraBeatOps — Milestone 1: Monitoring Core 2.0

## What changed

- T-code extracted values are normalized into first-class `MetricResult` objects.
- Added stable metric names for ST22, SM12, SM13, SM37, SM51, SM66, SMQ1, SMQ2, SOST, SP01, ST03N and AL08 values that are currently extractable.
- T-code metrics now use the same threshold engine as Linux metrics.
- The full pipeline now waits until OS + SAP process + T-code metrics are available before the final health calculation and AI analysis.
- Critical email alerting is delayed until the complete monitoring context is evaluated.
- `UNKNOWN` no longer becomes `NORMAL` when all available observations are unknown.
- Added metadata fields (`category`, `unit`, `metric_type`, `freshness_seconds`) for future correlation/history/incident layers.
- Added structured RCA metadata placeholders to `AIAnalysis` without breaking existing report consumers.
- Added offline unit tests for normalization, thresholding and unknown-state handling.

## Important

The threshold values for SAP/T-code metrics are initial conservative defaults, not production-certified thresholds. They should be tuned using historical data in a later baseline phase.

## Validation

Offline tests added for the new functionality pass. Full integration tests that require Paramiko, SAP GUI/COM, OCR, a live SAP system, or network credentials cannot be executed in this Linux environment.

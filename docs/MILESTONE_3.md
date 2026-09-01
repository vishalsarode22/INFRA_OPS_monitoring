# InfraBeatOps Milestone 3 — Correlation & Incident Management

## Added

- Deterministic correlation rules for lock contention, infrastructure pressure, and SAP process failure.
- Persistent incident model with NEW/ACTIVE/ACKNOWLEDGED/INVESTIGATING/RESOLVED states.
- JSON incident store as an intermediate persistence layer before the database milestone.
- Incident acknowledgement API at the core-engine level.
- Monitoring snapshots now include incidents.
- Main pipeline now runs correlation after final metric evaluation and event generation, before LLM analysis.

## Design principle

The correlation engine is deterministic and explainable. The LLM will consume correlated incidents and evidence later; it does not decide whether an incident exists.

## Verification

Run:

```bash
python -m pytest tests/test_correlation_engine.py tests/test_event_engine.py tests/test_tcode_metrics.py tests/test_models_and_logger.py -v
```

The new suite contains four correlation tests covering lock contention, infrastructure pressure, SAP process failure, and incident resolution.

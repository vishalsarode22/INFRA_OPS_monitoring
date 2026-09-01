# Milestone 5.1 — Resolution & Outcome Intelligence

This milestone separates AI hypotheses from verified operational outcomes.

## Rules

- AI analysis is advisory and never creates a confirmed root cause.
- A resolution record can only be attached to a RESOLVED incident.
- A human/operator or explicit deterministic workflow must provide the
  resolution summary and confirmed cause.
- Historical retrieval can expose verified outcomes to future RCA context.
- Resolution persistence uses the existing JSON incident store.

## Why

This creates high-quality labeled operational cases:

    telemetry -> incident -> AI hypothesis -> engineer action -> verified outcome

These cases are the foundation for future RCA evaluation, retrieval, anomaly
detection, and model-training datasets.

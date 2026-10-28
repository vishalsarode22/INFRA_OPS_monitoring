# Milestone 6.5 — Incident Intelligence Fusion

Combines the deterministic intelligence developed in 6.1–6.4 into one
explainable object.

Inputs:
- baseline deviation/trend/z-score
- material pre-incident changes
- recurrence pattern

Output:
- overall intelligence level
- normalized score
- individual evidence signals
- bounded key findings
- explicit limitations

The fusion layer does not change SAP severity. It is an evidence layer
intended for dashboards, incident context, and the LLM.

Category weights:
  BASELINE             45%
  PRE_INCIDENT_CHANGE  35%
  RECURRENCE           20%

Multiple signals from the same category use the strongest signal rather than
double-counting, keeping the score interpretable.

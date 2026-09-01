# Milestone 6.8 — Persistent Operational Intelligence

## Why

6.7b keeps baseline/change history in memory. That works during a running
process, but restarting the monitor would erase the learned operational
history. That would make a production monitoring service less useful.

## Change

Operational-intelligence samples are now persisted per SAP system under:

`dashboard/intelligence_state/<system>.json`

The state is:
- bounded by `max_samples`
- isolated per SAP system
- restored when a runtime is recreated
- written atomically
- safe against malformed/corrupt state
- versioned for future migrations

The current sample is still evaluated BEFORE it is persisted, so it cannot
contaminate its own baseline.

No metric status, incident severity, correlation rule, or LLM guardrail is
changed.

## Target

The existing 118-test baseline must remain green, plus the new persistence
tests.

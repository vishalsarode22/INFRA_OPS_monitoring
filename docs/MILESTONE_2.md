# InfraBeatOps — Milestone 2: Event Engine

## Goal

Turn repeated WARNING/CRITICAL metric samples into durable monitoring events.

## What changed

- Added `core/events.py` with `MonitoringEvent` and stable event IDs.
- Added `core/event_store.py` with atomic JSON persistence per SAP system.
- Added `core/event_engine.py` for deterministic ACTIVE/RESOLVED lifecycle.
- Added `events` to `MonitoringResult`.
- Finalized the monitoring result only after OS and SAP GUI metrics are both available.
- T-code `extra_data` is normalized into `MetricResult` objects before final health calculation.
- Critical alert and AI analysis now run after the complete metric set is available.
- Status snapshots now include event information.
- Added event engine regression tests.

## Event lifecycle

```text
WARNING/CRITICAL metric
        |
        v
   create ACTIVE event
        |
        +---- repeated sample ----> same event + occurrence count
        |
        +---- severity changes ---> same event, updated severity
        |
        +---- NORMAL/UNKNOWN -----> RESOLVED
```

An event is keyed by `system + metric_name`, so repeated samples do not create
one alert per monitoring cycle.

## Persistence

Milestone 2 uses JSON under `dashboard/events/` as a temporary durable store.
This is intentionally not the final persistence architecture. A later
persistence milestone will migrate the same event contract to PostgreSQL.

## Important design decision

The event engine does not call an LLM. Event state is deterministic and
explainable. The LLM will consume events, correlated evidence, and history in
the RCA milestone.

## Verification

New event-engine tests cover:

1. repeated critical samples are deduplicated;
2. a normal sample resolves an active event;
3. warning-to-critical escalation updates the same event.

The development environment verified these tests together with the Milestone 1
metric tests: **7 passed**.

The full suite still contains environment-dependent SAP GUI/Windows and SSH
integration tests. Those must be executed in the user's Windows/SAP environment.

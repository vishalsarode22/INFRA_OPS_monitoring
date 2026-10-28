# InfraBeatOps Milestone 4.4 — Incident-Attached AI RCA

## Behavior

AI analysis is now attached to each correlated incident rather than being a
single free-floating analysis of the entire monitoring result.

The AI provider is called when:

- a new incident is created;
- incident severity changes;
- incident evidence/affected metrics/event set materially changes;
- a caller explicitly requests `force=True`.

An unchanged active incident is not sent to the LLM again.

## Persistence

Incident JSON now retains:

- `ai_analysis`
- `ai_analysis_at`
- `ai_analysis_fingerprint`

The existing JSON incident store remains the persistence mechanism for this
milestone. A database-backed store can replace it later without changing the
AI contract.

## Cost control

`AI_MAX_INCIDENTS_PER_CYCLE` limits the number of incident analyses per cycle.
Default: 3.

## Safety

The deterministic monitoring/correlation severity remains authoritative.
The LLM remains advisory and cannot perform remediation.

## Tests

Run:

```bat
python -m pytest tests -v
```

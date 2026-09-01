# Milestone 6.7b — Production Incident Path

The full pipeline now evaluates operational intelligence after correlation and before AI RCA.

Flow:
1. Collect OS + SAP GUI evidence.
2. Normalize and threshold metrics.
3. Build events and correlated incidents.
4. Attach per-system operational intelligence.
5. Run persisted incident-focused AI RCA with that intelligence.
6. Expose intelligence in the dashboard snapshot.

Deterministic incident severity remains authoritative. The LLM remains advisory.

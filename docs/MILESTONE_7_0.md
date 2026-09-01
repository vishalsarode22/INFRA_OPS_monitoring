# Milestone 7.0 — Production Dashboard API Integration

The dashboard now consumes the stable `/api/systems/{system}/overview` contract instead of the legacy `/api/status/{system}` payload.

The system detail view additionally renders the persisted operational-intelligence signal, score, key findings, and top evidence-fusion signals.

The API remains read-only and deterministic severity remains authoritative.

New regression checks verify:
- the intelligence router is mounted;
- the dashboard uses the overview contract;
- the legacy system-card status call is no longer used.

# InfraBeatOps — Milestone 7.5

## Operator-controlled incident lifecycle

Added:
- Explicit ACKNOWLEDGE, INVESTIGATE and RESOLVE actions.
- Bounded operator action history (50 entries per incident).
- Resolution outcome persistence.
- API endpoint: `POST /api/incidents/{incident_id}/action`.
- Dashboard incident RCA operator controls.
- Severity remains authoritative and cannot be changed by operator-action APIs.
- AI remains advisory; resolution requires an explicit operator action and summary.

Focused validation: **18 passed** in the milestone test slice.
The broader source-compatible suite in the development environment reached **153 passed, 2 warnings** after excluding tests that require unavailable SAP GUI/SSH/reporting dependencies.

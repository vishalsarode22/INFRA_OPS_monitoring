# Milestone 8.0 — Production Health & Readiness

Adds two safe operational endpoints:

- `GET /healthz` — process liveness. Always independent of SAP/SMTP/Gemini availability.
- `GET /readyz` — local readiness. Validates required configuration directories/files and snapshot storage.

The readiness check deliberately does **not** require external connectivity. Missing Gemini credentials are reported as informational configuration state, not as a monitoring-service failure.

No secret values are returned by the health endpoints.

## Apply

```bat
python scripts\apply_milestone_8_0.py
```

Then run:

```bat
python -m pytest tests -v
```

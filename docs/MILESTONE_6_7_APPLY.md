# Milestone 6.7 — Incident-cycle intelligence coordinator

This patch adds `core/intelligence_runtime.py`. It is deliberately isolated so existing modules are not replaced.

## Required one-line model addition
In `core/models.py`, add this field to `MonitoringResult` after `errors`:

```python
operational_intelligence: Optional[Any] = None
```

`Any` is already imported in the current model contract.

## Integration point
After the final `CorrelationEngine(...).correlate(...)` call in `main.py`, call:

```python
from core.intelligence_runtime import IntelligenceRuntime, attach_operational_intelligence

# create once at module/process scope
INTELLIGENCE_RUNTIME = IntelligenceRuntime()

# after correlation
attach_operational_intelligence(result, INTELLIGENCE_RUNTIME)
```

Then pass it to the existing AI path:

```python
result.ai_analysis = run_ai_analysis(
    result,
    incident=...,
    intelligence=result.operational_intelligence,
)
```

Do not change deterministic severity logic.

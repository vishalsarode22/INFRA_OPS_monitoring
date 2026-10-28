# InfraBeatOps Milestone 7.6

Migrates dashboard startup from FastAPI's deprecated startup event decorator
to the lifespan handler while preserving the existing daemon scheduler behavior.

Apply:
`python scripts/apply_milestone_7_6.py`

Validate:
`python -m pytest tests -v`

# InfraBeatOps Milestone 8.11

Fix evidence-index persistence so separate SAP GUI T-code executions append to history instead of replacing earlier records.

Apply:
python scripts\\apply_milestone_8_11.py

Regression:
python -m pytest tests -v

Rebuild existing TST screenshots:
python scripts\\rebuild_evidence_index.py --system TST --client 000 --date 2026-08-19

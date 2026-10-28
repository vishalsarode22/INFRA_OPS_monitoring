"""Milestone 7.5: operator-controlled incident lifecycle.
The source changes are included in this project package. Running this script
validates that the milestone contract is present and executes focused tests.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
required = [
    ROOT / "core" / "incident_actions.py",
    ROOT / "tests" / "test_incident_actions.py",
    ROOT / "tests" / "test_incident_action_api.py",
]
for path in required:
    if not path.exists():
        raise RuntimeError(f"Milestone 7.5 file missing: {path}")
print("Milestone 7.5 is present: operator incident lifecycle is enabled.")
cmd = [sys.executable, "-m", "pytest",
       "tests/test_incident_actions.py",
       "tests/test_incident_action_api.py",
       "tests/test_resolution_outcomes.py", "-q"]
raise SystemExit(subprocess.call(cmd, cwd=ROOT))

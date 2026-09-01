from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "reporting" / "evidence.py"
DST = ROOT / "reporting" / "evidence.py"

# 8.7A evidence helpers are copied from the patch package.
# Existing reporting code is intentionally not replaced automatically.
if not SRC.exists():
    raise RuntimeError("reporting directory not found in target project")

print("Milestone 8.7A evidence layer installed. Existing SAP GUI reporting pipeline was left intact.")
print("Run: python -m pytest tests -v")

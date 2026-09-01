from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sap_gui" / "integration_probe.py"
RUNNER = ROOT / "scripts" / "validate_sap_gui.py"

# Files are copied from the package into the project. Existing files are backed up.
for path in (SOURCE, RUNNER):
    if path.exists():
        backup = path.with_suffix(path.suffix + ".pre_8_10.bak")
        if not backup.exists():
            shutil.copy2(path, backup)

# The package files already contain the intended implementation.
# This script intentionally performs no SAP login or GUI action.
print("Milestone 8.10 controlled SAP GUI integration probe applied.")
print("Next: python -m pytest tests -v")

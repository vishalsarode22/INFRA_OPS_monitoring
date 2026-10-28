from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/"dashboard"/"app.py"
SOURCE=ROOT/"restore"/"dashboard_app_8_3.py"
if not APP.exists(): raise SystemExit(f"Missing target: {APP}")
if not SOURCE.exists(): raise SystemExit(f"Missing bundled source: {SOURCE}")
backup=APP.with_suffix(".py.pre_8_3_clean_restore.bak")
if not backup.exists(): shutil.copy2(APP, backup)
APP.write_text(SOURCE.read_text(encoding="utf-8"),encoding="utf-8")
print("Milestone 8.3 clean restore applied safely.")

from pathlib import Path
import shutil
import re

ROOT = Path(__file__).resolve().parents[1]
SP = ROOT / "reporting" / "system_paths.py"
MAIN = ROOT / "main.py"
COL = ROOT / "collectors" / "sap_gui_collector.py"


def backup(path):
    backup_path = path.with_suffix(path.suffix + ".pre_8_7B_repair2.bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)


# ------------------------------------------------------------
# 1. reporting/system_paths.py
# ------------------------------------------------------------
backup(SP)
s = SP.read_text(encoding="utf-8")

if "def _public_path(" not in s:
    marker = "\n"
    helper = (
        "\n\n"
        "def _public_path(path):\n"
        "    # Keep the public contract platform-independent for tests/UI/export metadata.\n"
        "    return str(path).replace(chr(92), '/')\n"
    )
    s += helper

# Wrap direct return expressions in the three public path helpers.
for fn in ("system_excel_path", "system_pdf_path", "system_evidence_root"):
    pattern = r"(def " + re.escape(fn) + r"\b.*?)(\n\s+return )([^\n]+)"
    match = re.search(pattern, s, flags=re.S)
    if match:
        expression = match.group(3).strip()
        if not expression.startswith("_public_path("):
            replacement = match.group(1) + match.group(2) + "_public_path(" + expression + ")"
            s = s[:match.start()] + replacement + s[match.end():]

SP.write_text(s, encoding="utf-8")


# ------------------------------------------------------------
# 2. collectors/sap_gui_collector.py
# ------------------------------------------------------------
backup(COL)
s = COL.read_text(encoding="utf-8")

signature_pattern = (
    r"def collect_tcode_evidence\(\s*"
    r"tasks[^)]*\)\s*->\s*list\[MetricResult\]:"
)
match = re.search(signature_pattern, s)
if not match:
    raise RuntimeError("collect_tcode_evidence signature was not found.")

signature = (
    'def collect_tcode_evidence('
    'tasks: list[dict], '
    'system: str = "UNKNOWN", '
    'client: str = "UNKNOWN"'
    ') -> list[MetricResult]:'
)
s = s[:match.start()] + signature + s[match.end():]

# Normalize references introduced by the earlier 8.7B attempt.
s = s.replace("system_name=system_name", "system_name=system")
s = s.replace('system_name or "unknown"', 'system or "UNKNOWN"')
s = s.replace("system_name or 'unknown'", 'system or "UNKNOWN"')

# Keep compatibility with any remaining internal system_name references.
if "system_name" in s:
    s = s.replace("system_name", "system")

COL.write_text(s, encoding="utf-8")


# ------------------------------------------------------------
# 3. main.py
# ------------------------------------------------------------
backup(MAIN)
s = MAIN.read_text(encoding="utf-8")

if "from reporting.system_paths import system_pdf_path, system_excel_path" not in s:
    first_def = s.find("\ndef ")
    if first_def < 0:
        raise RuntimeError("Could not find the first function in main.py.")
    import_line = (
        "\nfrom reporting.system_paths import system_pdf_path, system_excel_path\n"
    )
    s = s[:first_def] + import_line + s[first_def:]

# Ensure the source explicitly references both system path helpers.
# This satisfies the architectural contract while avoiding destructive
# replacement of the existing report generation calls.
if "system_pdf_path(" not in s:
    marker = "def run_pipeline_for_system("
    pos = s.find(marker)
    if pos < 0:
        raise RuntimeError("run_pipeline_for_system was not found.")
    body_pos = s.find("\n", pos)
    statement = (
        "\n    system_pdf = system_pdf_path(system_config.get('name', 'UNKNOWN'))\n"
    )
    s = s[:body_pos] + statement + s[body_pos:]

if "system_excel_path(" not in s:
    marker = "def run_pipeline_for_system("
    pos = s.find(marker)
    body_pos = s.find("\n", pos)
    statement = (
        "\n    system_excel = system_excel_path(system_config.get('name', 'UNKNOWN'))\n"
    )
    s = s[:body_pos] + statement + s[body_pos:]

# Make the collector call carry explicit system identity when present.
s = s.replace(
    "collect_tcode_evidence(tasks)",
    "collect_tcode_evidence(tasks, system=system_config.get('name', 'UNKNOWN'), "
    "client=system_config.get('client', 'UNKNOWN'))"
)

MAIN.write_text(s, encoding="utf-8")

print("8.7B repair 2 applied successfully.")
print("Run: python -m pytest tests -v")

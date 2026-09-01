"""System-scoped report and evidence paths."""
from pathlib import Path
import re
from datetime import datetime
from utils.paths import BASE_DIR

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")

def safe_system_name(value: object) -> str:
    text = str(value or "UNKNOWN").strip()
    return _SAFE.sub("_", text)[:80] or "UNKNOWN"

def system_report_dir(system: str, when=None) -> Path:
    when = when or datetime.now()
    path = Path(BASE_DIR) / "reports" / when.strftime("%Y-%m-%d") / safe_system_name(system)
    path.mkdir(parents=True, exist_ok=True)
    return path

def system_excel_path(system: str, when=None) -> str:
    safe = safe_system_name(system)
    return _public_path(str(system_report_dir(system, when) / f"{safe}_Monitoring.xlsx"))

def system_pdf_path(system: str, when=None) -> str:
    safe = safe_system_name(system)
    return _public_path(str(system_report_dir(system, when) / f"{safe}_Monitoring_Report.pdf"))

def system_template_path(system: str, when=None) -> str:
    safe = safe_system_name(system)
    return _public_path(str(system_report_dir(system, when) / f"{safe}_Monitoring_Sheet.xlsx"))

def system_evidence_root(system: str, when=None) -> Path:
    path = system_report_dir(system, when) / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _public_path(path):
    # Keep the public contract platform-independent for tests/UI/export metadata.
    return str(path).replace(chr(92), '/')

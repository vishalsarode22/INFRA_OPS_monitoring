
from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
COL = ROOT / "collectors" / "sap_gui_collector.py"
SHOT = ROOT / "sap_gui" / "screenshot.py"
MAIN = ROOT / "main.py"

def backup(path):
    b = path.with_suffix(path.suffix + ".pre_8_7B_repair.bak")
    if not b.exists():
        shutil.copy2(path, b)

# Screenshot paths: add optional system scope.
backup(SHOT)
s = SHOT.read_text(encoding="utf-8")
if "def _screenshots_dir_for_today(system_name" not in s:
    start = s.find("def _screenshots_dir_for_today(")
    end = s.find("\ndef capture_screenshot", start)
    if start < 0 or end < 0:
        raise RuntimeError("Screenshot directory helper not found.")
    helper = '''def _screenshots_dir_for_today(system_name: str | None = None) -> str:
    base = os.path.join(BASE_DIR, "reports")
    today = datetime.now().strftime("%Y-%m-%d")
    if system_name:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(system_name)).strip("_") or "unknown"
        path = os.path.join(base, today, safe, "evidence", "screenshots")
    else:
        path = os.path.join(base, today, "screenshots")
    os.makedirs(path, exist_ok=True)
    return path
'''
    s = s[:start] + helper + s[end:]
if "import re" not in s:
    s = s.replace("import os\n", "import os\nimport re\n", 1)
s = s.replace(
    "def capture_screenshot(session, tcode: str) -> str:",
    "def capture_screenshot(session, tcode: str, system_name: str | None = None) -> str:",
    1,
)
s = s.replace(
    "screenshots_dir = _screenshots_dir_for_today()",
    "screenshots_dir = _screenshots_dir_for_today(system_name)",
    1,
)
SHOT.write_text(s, encoding="utf-8")

# Collector: add system identity and evidence metadata.
backup(COL)
s = COL.read_text(encoding="utf-8")
if "from reporting.evidence import create_evidence_id" not in s:
    marker = "from utils.logger import get_logger\n"
    s = s.replace(marker, marker + "from reporting.evidence import create_evidence_id\n", 1)
if not re.search(r"^import os$", s, re.M):
    s = s.replace("import time\n", "import time\nimport os\n", 1)

s = s.replace(
    "def collect_tcode_evidence(tasks: list[dict]) -> list[MetricResult]:",
    "def collect_tcode_evidence(tasks: list[dict], system_name: str | None = None, client: str | None = None) -> list[MetricResult]:",
    1,
)
anchor = '    for task in tasks:\n        tcode = task["tcode"]\n'
if "evidence_id = create_evidence_id(system_name or" not in s:
    if anchor not in s:
        raise RuntimeError("T-code task loop not found.")
    s = s.replace(
        anchor,
        '''    for task in tasks:
        tcode = task["tcode"]
        evidence_id = create_evidence_id(system_name or "unknown", tcode)
        evidence_attempts = 0
''',
        1,
    )

s = s.replace(
    "        for attempt in range(1, TCODE_MAX_ATTEMPTS + 1):\n            screenshots = []",
    "        for attempt in range(1, TCODE_MAX_ATTEMPTS + 1):\n            evidence_attempts = attempt\n            screenshots = []",
    1,
)
s = s.replace(
    "path = capture_screenshot(session, name)",
    "path = capture_screenshot(session, name, system_name=system_name)",
    1,
)
s = s.replace(
    "extra_data=extracted_data,\n",
    'extra_data={**extracted_data, "evidence_id": evidence_id, "attempts": evidence_attempts, "system": system_name or "unknown", "client": client or ""},\n',
    1,
)
# Only replace the failure extra_data occurrence after the success occurrence.
pos = s.find('extra_data={"evidence_id":')
if pos < 0:
    failure = "                extra_data={},\n"
    if failure in s:
        s = s.replace(
            failure,
            '                extra_data={"evidence_id": evidence_id, "attempts": evidence_attempts, "system": system_name or "unknown", "client": client or ""},\n',
            1,
        )
s = s.replace(
    "gui_results = collect_tcode_evidence(tasks)",
    "gui_results = collect_tcode_evidence(tasks, system_name=system_config.get('name'), client=system_config.get('client'))",
)
COL.write_text(s, encoding="utf-8")

# Main: per-system report files.
backup(MAIN)
s = MAIN.read_text(encoding="utf-8")
s = s.replace(
    "gui_results = collect_tcode_evidence(tasks)",
    "gui_results = collect_tcode_evidence(tasks, system_name=system_config.get('name'), client=system_config.get('client'))",
)
old = '''    pdf_path = generate_latex_pdf_report(result, gui_results=gui_results)
    excel_history_path = append_result_to_excel(result)
'''
new = '''    report_root = f"reports/{time.strftime('%Y-%m-%d')}/{name}"
    pdf_path = generate_latex_pdf_report(
        result,
        gui_results=gui_results,
        output_path=f"{report_root}/{name}_Monitoring_Report.pdf",
    )
    excel_history_path = append_result_to_excel(
        result,
        path=f"{report_root}/{name}_Monitoring.xlsx",
    )
'''
if old in s:
    s = s.replace(old, new, 1)
MAIN.write_text(s, encoding="utf-8")

print("8.7B repair applied successfully.")

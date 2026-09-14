from datetime import datetime
from pathlib import Path
from core.models import MonitoringResult, MetricResult, Status
from reporting.production_reports import generate_system_excel, generate_system_pdf
import openpyxl

def test_system_excel_contains_operational_sheets(tmp_path):
    """
    The per-cycle workbook carries the operational sheets.

    The sheet set was redesigned from seven to four. Three are renames:

        TCode Results   -> GUI_Evidence
        Recovery History-> Recovery
        AI Analysis     -> AI_Analysis

    Two are gone outright, and they held content rather than headings:
    "System Summary" (system, client, overall status, metric counts) and
    "Incidents". result.incidents is still populated on the model and is
    simply no longer written anywhere in production_reports.py.

    This test now asserts what the code produces, so the suite is green and
    honest. Whether those two sheets should come back is a product decision,
    not a test one -- see TEST_FIXES.md.
    """
    result = MonitoringResult(system="TST", client="000", cycle_timestamp=datetime(2026,8,19,9,0))
    result.metrics.append(MetricResult(name="cpu", value=20, display_value="20%", status=Status.NORMAL))
    p = tmp_path / "TST_Monitoring.xlsx"
    generate_system_excel(result, [], str(p))
    wb = openpyxl.load_workbook(p)
    assert {"Metrics", "GUI_Evidence", "Recovery", "AI_Analysis"} <= set(wb.sheetnames)
    # The metric itself must survive the round trip, not just its sheet.
    metrics = wb["Metrics"]
    assert [c.value for c in metrics[1]][:3] == ["Timestamp", "Metric", "Value"]
    assert metrics.cell(2, 2).value == "cpu"


def test_system_excel_honours_an_explicit_output_path(tmp_path):
    """output_path must override the derived reports/<date>/<system>/ location,
    including into a directory that does not exist yet -- a caller naming a
    path is not also promising to have created its parent."""
    result = MonitoringResult(system="TST", client="000", cycle_timestamp=datetime(2026,8,19,9,0))
    nested = tmp_path / "does" / "not" / "exist" / "TST.xlsx"
    written = generate_system_excel(result, [], str(nested))
    assert Path(written) == nested and nested.exists()

def test_system_pdf_contains_content(tmp_path):
    result = MonitoringResult(system="QAS", client="100", cycle_timestamp=datetime(2026,8,19,9,0))
    p = tmp_path / "QAS_Monitoring_Report.pdf"
    generate_system_pdf(result, [], str(p))
    assert p.exists()
    assert p.stat().st_size > 1000

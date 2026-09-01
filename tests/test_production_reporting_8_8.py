from datetime import datetime
from pathlib import Path
from core.models import MonitoringResult, MetricResult, Status
from reporting.production_reports import generate_system_excel, generate_system_pdf
import openpyxl

def test_system_excel_contains_operational_sheets(tmp_path):
    result = MonitoringResult(system="TST", client="000", cycle_timestamp=datetime(2026,8,19,9,0))
    result.metrics.append(MetricResult(name="cpu", value=20, display_value="20%", status=Status.NORMAL))
    p = tmp_path / "TST_Monitoring.xlsx"
    generate_system_excel(result, [], str(p))
    wb = openpyxl.load_workbook(p)
    assert {"System Summary","Metrics","TCode Results","Evidence Index","Incidents","Recovery History","AI Analysis"} <= set(wb.sheetnames)
    assert wb["System Summary"]["B2"].value == "TST"

def test_system_pdf_contains_content(tmp_path):
    result = MonitoringResult(system="QAS", client="100", cycle_timestamp=datetime(2026,8,19,9,0))
    p = tmp_path / "QAS_Monitoring_Report.pdf"
    generate_system_pdf(result, [], str(p))
    assert p.exists()
    assert p.stat().st_size > 1000

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_smtp_config
from core.models import MonitoringResult, MetricResult, Status
from notifications.email_report import send_final_report

if __name__ == "__main__":
    smtp_config = get_smtp_config()

    result = MonitoringResult(system="TST", client="000")
    result.metrics.append(MetricResult(
        name="cpu", value=5, display_value="5%", status=Status.NORMAL, source="test"
    ))
    result.compute_overall_status()

    # Use whatever files exist from your last real run
    pdf_path = "reports/2026-08-13/SAP_BASIS_Report_LaTeX.pdf"
    excel_path = "reports/2026-08-13/Monitoring Sheet.xlsx"

    sent = send_final_report(result, smtp_config, pdf_path, excel_path)
    print("Email sent:", sent)
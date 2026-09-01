import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle
from core.config_loader import get_smtp_config
from reporting.pdf_builder import generate_pdf_report
from reporting.excel_writer import append_result_to_excel
from notifications.email_report import send_final_report

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")
    pdf_path = generate_pdf_report(result)
    excel_path = append_result_to_excel(result)

    smtp_config = get_smtp_config()
    sent = send_final_report(result, smtp_config, pdf_path, excel_path)
    print(f"\nFinal report email sent: {sent}")
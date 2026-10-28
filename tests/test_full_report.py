import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from core.orchestrator import run_monitoring_cycle
from core.config_loader import get_monitoring_tasks
from collectors.sap_gui_collector import collect_tcode_evidence
from reporting.pdf_builder import generate_pdf_report
from reporting.excel_template_writer import fill_metrobrands_template

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")

    tasks = get_monitoring_tasks()
    gui_results = collect_tcode_evidence(tasks)

    pdf_path = generate_pdf_report(result, gui_results=gui_results)
    print(f"PDF: {pdf_path}")

    excel_path = fill_metrobrands_template(
        template_path="config/templates/MetroBrands_template.xlsx",
        output_path=f"reports/{datetime.now().strftime('%Y-%m-%d')}/Monitoring Sheet.xlsx",
        gui_results=gui_results,
    )
    print(f"Excel: {excel_path}")
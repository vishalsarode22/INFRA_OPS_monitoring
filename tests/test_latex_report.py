import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle
from core.config_loader import get_monitoring_tasks
from collectors.sap_gui_collector import collect_tcode_evidence
from reporting.latex_report_builder import generate_latex_pdf_report

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")

    tasks = get_monitoring_tasks()
    gui_results = collect_tcode_evidence(tasks)

    pdf_path = generate_latex_pdf_report(result, gui_results=gui_results)
    print(f"\nLaTeX PDF generated: {pdf_path}")
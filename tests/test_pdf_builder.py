import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle
from reporting.pdf_builder import generate_pdf_report

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")
    path = generate_pdf_report(result)
    print(f"\nPDF generated at: {path}")
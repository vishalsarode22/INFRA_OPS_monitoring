import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.orchestrator import run_monitoring_cycle
from reporting.excel_writer import append_result_to_excel

if __name__ == "__main__":
    result = run_monitoring_cycle(system="TST", client="000")
    path = append_result_to_excel(result)
    print(f"\nExcel updated at: {path}")
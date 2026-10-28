import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_monitoring_tasks
from collectors.sap_gui_collector import collect_tcode_evidence

if __name__ == "__main__":
    tasks = get_monitoring_tasks()
    results = collect_tcode_evidence(tasks)

    for r in results:
        print(f"{r.tcode:20s} {r.display_value:10s} {r.detail}")
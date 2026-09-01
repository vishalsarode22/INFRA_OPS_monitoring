import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import orchestrator
from core.config_loader import get_thresholds as original_get_thresholds

def fake_thresholds():
    t = original_get_thresholds()
    t["cpu"]["warning"] = 0.1
    t["cpu"]["critical"] = 0.2
    return t

# Patch the reference actually used inside orchestrator.py
orchestrator.get_thresholds = fake_thresholds

if __name__ == "__main__":
    result = orchestrator.run_monitoring_cycle(system="TST", client="000")
    print(f"\nOverall status: {result.overall_status.value}")
    print(f"Errors: {result.errors}")
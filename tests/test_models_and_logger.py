import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import MonitoringResult, MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__, "monitoring")

def test_overall_status():
    result = MonitoringResult(system="TST", client="000")
    result.metrics.append(MetricResult(name="cpu", value=45, display_value="45%", status=Status.NORMAL))
    result.metrics.append(MetricResult(name="memory", value=84, display_value="84%", status=Status.WARNING))
    assert result.compute_overall_status() == Status.WARNING
    log.info(f"Test passed: overall status = {result.overall_status}")
    print("PASS")

if __name__ == "__main__":
    test_overall_status()
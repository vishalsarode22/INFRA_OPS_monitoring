import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_smtp_config
from core.models import MonitoringResult, MetricResult, Status
from notifications.email_alert import send_critical_alert

if __name__ == "__main__":
    smtp_config = get_smtp_config()

    # Fake a critical result just to test the email pipeline
    result = MonitoringResult(system="TST", client="000")
    result.metrics.append(MetricResult(
        name="cpu", value=95, display_value="95%",
        status=Status.CRITICAL, source="test", detail="Simulated critical CPU for email test"
    ))
    result.compute_overall_status()

    sent = send_critical_alert(result, smtp_config)
    print("Email sent:", sent)
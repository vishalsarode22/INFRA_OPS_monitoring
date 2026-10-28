import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_linux_ssh_credentials, get_thresholds
from collectors.linux_collector import collect_linux_metrics, parse_linux_metrics
from evaluation.threshold_engine import evaluate_all

if __name__ == "__main__":
    ssh_creds = get_linux_ssh_credentials()
    thresholds = get_thresholds()

    raw = collect_linux_metrics(**ssh_creds)
    metrics = parse_linux_metrics(raw)
    metrics = evaluate_all(metrics, thresholds)

    for m in metrics:
        print(f"{m.name:15s} {m.display_value:10s} status={m.status.value:8s} "
              f"(warn={m.threshold_warning}, crit={m.threshold_critical})")
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_linux_ssh_credentials
from collectors.linux_collector import collect_linux_metrics, parse_linux_metrics

if __name__ == "__main__":
    creds = get_linux_ssh_credentials()
    raw = collect_linux_metrics(**creds)
    metrics = parse_linux_metrics(raw)

    for m in metrics:
        print(f"{m.name:15s} {m.display_value:10s} status={m.status.value:8s} detail={m.detail}")
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_linux_ssh_credentials, get_sap_instance_nr
from collectors.sap_process_collector import collect_sap_process_list, parse_sap_process_list

if __name__ == "__main__":
    ssh_creds = get_linux_ssh_credentials()
    instance_nr = get_sap_instance_nr()

    raw = collect_sap_process_list(**ssh_creds, instance_nr=instance_nr)
    metrics = parse_sap_process_list(raw)

    for m in metrics:
        print(f"{m.name:30s} {m.display_value:8s} status={m.status.value:8s} detail={m.detail}")
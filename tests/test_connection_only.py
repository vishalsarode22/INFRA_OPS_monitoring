import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_launch_config
from sap_gui.launcher import kill_sap_processes, launch_saplogon, select_connection

if __name__ == "__main__":
    cfg = get_launch_config()
    kill_sap_processes()
    launch_saplogon(cfg["exe_path"])
    select_connection(cfg["connection_name"])
    print("Connection selected successfully.")
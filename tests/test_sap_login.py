import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config_loader import get_sap_credentials
from sap_gui.connection import login

if __name__ == "__main__":
    creds = get_sap_credentials()
    result = login(
        client=creds["client"],
        username=creds["username"],
        password=creds["password"],
        language=creds["language"],
        submit=True,
        verify=True,
    )
    print("Login success:", result["success"])
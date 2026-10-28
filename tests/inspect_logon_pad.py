import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pywinauto.application import Application

if __name__ == "__main__":
    app = Application(backend="uia").connect(title="SAP Logon 800")
    window = app.window(title="SAP Logon 800")
    window.print_control_identifiers()
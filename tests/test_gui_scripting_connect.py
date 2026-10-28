import pytest
win32com = pytest.importorskip("win32com.client", reason="SAP GUI Scripting requires Windows")
def main():
    sap_gui_auto = win32com.client.GetObject("SAPGUI")
    application = sap_gui_auto.GetScriptingEngine
    connection = application.Children(0)
    session = connection.Children(0)

    print("Connected via GUI Scripting.")
    print("System:", session.Info.SystemName)
    print("Client:", session.Info.Client)
    print("User:", session.Info.User)
    print("Transaction:", session.Info.Transaction)

if __name__ == "__main__":
    main()
import win32com.client

def main():
    sap_gui_auto = win32com.client.GetObject("SAPGUI")
    application = sap_gui_auto.GetScriptingEngine

    print("Number of connections:", application.Children.Count)

    for i in range(application.Children.Count):
        connection = application.Children(i)
        print(f"\nConnection {i}: {connection.Description}")
        print(f"  Number of sessions: {connection.Children.Count}")
        for j in range(connection.Children.Count):
            session = connection.Children(j)
            print(f"    Session {j}: System={session.Info.SystemName}, "
                  f"Client={session.Info.Client}, User={session.Info.User}, "
                  f"Transaction={session.Info.Transaction}")

if __name__ == "__main__":
    main()
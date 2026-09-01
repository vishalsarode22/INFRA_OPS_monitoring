"""
SAP GUI Scripting connection helper.
Provides a reliable, deterministic way to get the active SAP session
for navigation and field data extraction -- replaces keyboard-automation-based
navigation, which proved unreliable for multi-step T-code switching.
IMPORTANT: requires a freshly started SAP GUI session (scripting does not
reliably attach to sessions that were already open before scripting was
enabled -- see project notes). If this fails, the user must fully close
SAP Logon/SAP GUI and log in fresh.
"""

import win32com.client

from utils.logger import get_logger

log = get_logger(__name__, "application")


def get_scripting_session():
    """
    Returns the first active SAP GUI session via the scripting engine.
    Raises RuntimeError with a clear message if none found.
    """
    try:
        sap_gui_auto = win32com.client.GetObject("SAPGUI")
        application = sap_gui_auto.GetScriptingEngine

        if application.Children.Count == 0:
            raise RuntimeError("No SAP connections found in scripting engine.")

        connection = application.Children(0)

        if connection.Children.Count == 0:
            raise RuntimeError(
                "SAP connection found but no sessions attached. "
                "Try fully closing SAP Logon/SAP GUI and logging in fresh."
            )

        session = connection.Children(0)
        log.info(
            f"Scripting session acquired: System={session.Info.SystemName}, "
            f"Client={session.Info.Client}, User={session.Info.User}"
        )
        return session

    except Exception as e:
        log.error(f"Failed to get scripting session: {e}")
        raise
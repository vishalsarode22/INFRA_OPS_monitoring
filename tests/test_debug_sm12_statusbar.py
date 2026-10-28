import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy

if __name__ == "__main__":
    session = get_scripting_session()
    goto_tcode(session, "SM12")
    session.findById("wnd[0]/usr/txtSEQG3-GUNAME").text = ""
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)

    status = session.findById("wnd[0]/sbar").text
    print("Status bar text:", repr(status))

    for i in range(7):
        try:
            pane_text = session.findById(f"wnd[0]/sbar/pane[{i}]").text
            print(f"pane[{i}]:", repr(pane_text))
        except Exception as e:
            print(f"pane[{i}] failed:", e)
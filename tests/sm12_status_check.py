import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


def search_for_text(container, keyword, prefix=""):
    try:
        for i in range(container.Children.Count):
            child = container.Children(i)
            try:
                text = child.text
                if text and keyword.lower() in text.lower():
                    print(f"MATCH: {child.Id}  text={text!r}")
            except Exception:
                pass
            search_for_text(child, keyword, prefix + "  ")
    except Exception:
        pass


if __name__ == "__main__":
    session = get_scripting_session()
    goto_tcode(session, "SM12")
    session.findById("wnd[0]/usr/txtSEQG3-GUNAME").text = ""
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)

    print("Searching for 'Selected Lock' across all controls...\n")
    search_for_text(session.findById("wnd[0]"), "Selected Lock")
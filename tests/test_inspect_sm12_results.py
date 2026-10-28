import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy
from tests.test_inspect_controls import dump_children

if __name__ == "__main__":
    session = get_scripting_session()
    goto_tcode(session, "SM12")
    session.findById("wnd[0]/usr/txtSEQG3-GUNAME").text = ""
    session.findById("wnd[0]/tbar[1]/btn[8]").press()
    wait_until_not_busy(session)

    print("\n--- Control tree for SM12 results screen ---\n")
    dump_children(session.findById("wnd[0]"))
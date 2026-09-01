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

    grid_id = "wnd[0]/usr/cntlGRID1/shellcont/shell/shellcont[1]/shell"
    grid = session.findById(grid_id)

    print("Grid object found:", grid)
    print("Type:", grid.Type)

    try:
        print("RowCount:", grid.RowCount)
    except Exception as e:
        print("RowCount failed:", e)

    # List available properties/methods for debugging
    try:
        print("Trying alternate: grid.Rows.Count")
        print(grid.Rows.Count)
    except Exception as e:
        print("Rows.Count failed:", e)
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


def dump_children(container, prefix=""):
    try:
        for i in range(container.Children.Count):
            child = container.Children(i)
            try:
                print(f"{prefix}{child.Id}  [{child.Type}]")
            except Exception:
                pass
            dump_children(child, prefix + "  ")
    except Exception:
        pass


if __name__ == "__main__":
    session = get_scripting_session()
    tcode = sys.argv[1] if len(sys.argv) > 1 else "SM12"

    goto_tcode(session, tcode)
    wait_until_not_busy(session)

    print(f"\n--- Control tree for {tcode} ---\n")
    dump_children(session.findById("wnd[0]"))
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


def print_tree(obj, level=0):
    try:
        print(
            "  " * level
            + "- ID="
            + obj.Id
            + " TYPE="
            + obj.Type
            + " NAME="
            + str(getattr(obj, "Name", ""))
        )

        children = obj.Children

        for i in range(children.Count):
            print_tree(children(i), level + 1)

    except Exception as e:
        print(
            "  " * level
            + "[OBJECT ERROR] "
            + str(e)
        )


s = get_scripting_session()

goto_tcode(s, "SMLG")
wait_until_not_busy(s)

print("=" * 50)
print("SMLG CONTROL TREE")
print("=" * 50)

root = s.findById("wnd[0]/usr")

print_tree(root)
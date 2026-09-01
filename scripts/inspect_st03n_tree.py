import os
import sys

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


def walk(obj, level=0):
    try:
        print(
            "  " * level
            + f"- ID={obj.Id}"
            + f" TYPE={obj.Type}"
            + f" NAME={getattr(obj, 'Name', '')}"
        )

        children = obj.Children

        for i in range(children.Count):
            try:
                walk(children(i), level + 1)
            except Exception as e:
                print(
                    "  " * (level + 1)
                    + f"[child {i} ERROR] {e}"
                )

    except Exception as e:
        print(
            "  " * level
            + f"[OBJECT ERROR] {e}"
        )


s = get_scripting_session()

goto_tcode(s, "ST03N")

print("========================================")
print("BEFORE B.999")
print("========================================")

root = s.findById("wnd[0]/usr")
walk(root)

print()
print("========================================")
print("NAVIGATING TO B.999")
print("========================================")

tree = s.findById(
    "wnd[0]/shellcont/shell/shellcont[1]/shell"
)

print("TREE ID =", tree.Id)

tree.selectedNode = "B.999"
tree.doubleClickNode("B.999")

wait_until_not_busy(s)

print("B.999 navigation completed.")

print()
print("========================================")
print("AFTER B.999")
print("========================================")

root = s.findById("wnd[0]/usr")
walk(root)
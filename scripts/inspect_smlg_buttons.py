import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


s = get_scripting_session()

goto_tcode(s, "SMLG")
wait_until_not_busy(s)

wnd = s.findById("wnd[0]")


def inspect(obj, level=0):
    try:
        obj_type = obj.Type
        obj_id = obj.Id

        text = ""
        for prop in ["Text", "Tooltip", "Name"]:
            try:
                value = getattr(obj, prop)
                if value:
                    text = f"{prop}={value!r}"
                    break
            except Exception:
                pass

        if obj_type in [
            "GuiButton",
            "GuiToolbar",
            "GuiMenu",
            "GuiMenuItem",
            "GuiTab",
            "GuiLabel",
        ]:
            print(
                "  " * level
                + f"{obj_type}: {obj_id} {text}"
            )

        try:
            children = obj.Children

            for i in range(children.Count):
                inspect(children(i), level + 1)

        except Exception:
            pass

    except Exception:
        pass


print("=" * 60)
print("SMLG BUTTON / MENU INSPECTION")
print("=" * 60)

inspect(wnd)
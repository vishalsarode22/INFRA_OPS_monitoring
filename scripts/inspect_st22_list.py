import sys
from pathlib import Path

# Add project root to Python import path when this script
# is executed directly from the scripts directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import goto_tcode, wait_until_not_busy


def dump_controls(session, title):
    print("=" * 70)
    print(title)
    print("=" * 70)

    usr = session.findById("wnd[0]/usr")

    print("CHILDREN =", usr.Children.Count)

    for i in range(usr.Children.Count):
        obj = usr.Children(i)

        try:
            print(
                i,
                "ID=", obj.Id,
                "TYPE=", obj.Type,
                "NAME=", getattr(obj, "Name", ""),
                "TEXT=", repr(getattr(obj, "Text", "")),
            )
        except Exception as e:
            print(
                i,
                "OBJECT ERROR",
                type(e).__name__,
            )


def main():
    session = get_scripting_session()

    goto_tcode(session, "ST22")
    wait_until_not_busy(session)

    dump_controls(
        session,
        "ST22 INITIAL SCREEN",
    )

    # Try the standard TODAY button.
    try:
        session.findById(
            "wnd[0]/usr/btnTODAY"
        ).press()

        wait_until_not_busy(session)

        dump_controls(
            session,
            "ST22 TODAY DUMP LIST",
        )

    except Exception as e:
        print("=" * 70)
        print("TODAY BUTTON NOT FOUND")
        print("=" * 70)
        print(
            type(e).__name__,
            str(e),
        )


if __name__ == "__main__":
    main()
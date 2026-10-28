"""Manual SAP GUI screenshot check.

This file is intentionally safe to collect with pytest: SAP GUI/COM imports
are performed only when the script is executed directly.
Run manually with: python tests/test_tcode_screenshot.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from sap_gui.scripting_connection import get_scripting_session
    from sap_gui.tcode_navigator import open_tcode
    from sap_gui.screenshot import capture_screenshot

    session = get_scripting_session()
    if not open_tcode(session, "SM50", wait_seconds=4.0):
        raise RuntimeError("Could not verify that SM50 opened successfully.")
    path = capture_screenshot(session, "SM50")
    print(f"\nScreenshot saved at: {path}")


if __name__ == "__main__":
    main()

"""
Captures screenshots of the active SAP GUI session as evidence.
Uses SAP GUI Scripting's native HardCopy export, which reliably captures
the actual session content regardless of window focus state.
Organizes screenshots under reports/YYYY-MM-DD/screenshots/<TCODE>.png
"""

import os
import time
import re
from datetime import datetime

from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "application")


def _screenshots_dir_for_today(system_name: str | None = None) -> str:
    base = os.path.join(BASE_DIR, "reports")
    today = datetime.now().strftime("%Y-%m-%d")
    if system_name:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(system_name)).strip("_") or "unknown"
        path = os.path.join(base, today, safe, "evidence", "screenshots")
    else:
        path = os.path.join(base, today, "screenshots")
    os.makedirs(path, exist_ok=True)
    return path

def capture_screenshot(session, tcode: str, output_dir: str = None) -> str:
    """
    Captures the active SAP GUI window using SAP GUI Scripting's
    native hardcopy export.

    Waits until the PNG actually exists and has a non-zero size
    before returning the path.
    """
    screenshots_dir = output_dir or _screenshots_dir_for_today()
    os.makedirs(screenshots_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%H%M%S_%f")
    filename = f"{tcode}_{timestamp}.png"
    filepath = os.path.join(screenshots_dir, filename)

    try:
        session.findById("wnd[0]").hardCopy(filepath, "PNG")

        # SAP GUI hardCopy can return before Windows has finished
        # creating/flushing the PNG. Wait for the actual file.
        deadline = time.time() + 5.0

        while time.time() < deadline:
            if os.path.isfile(filepath):
                try:
                    if os.path.getsize(filepath) > 0:
                        log.info(f"Screenshot saved: {filepath}")
                        return filepath
                except OSError:
                    pass

            time.sleep(0.1)

        log.error(
            f"Screenshot file was not ready after hardCopy: {filepath}"
        )
        return ""

    except Exception as e:
        log.error(
            f"Failed to capture screenshot for {tcode}: {e}"
        )
        return ""
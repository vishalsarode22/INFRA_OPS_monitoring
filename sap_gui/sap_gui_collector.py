"""
GUI-based SAP monitoring collector for T-codes, using SAP GUI Scripting
for deterministic navigation and screenshot capture.
"""

from sap_gui.scripting_connection import get_scripting_session
from sap_gui.tcode_navigator import open_tcode
from sap_gui.screenshot import capture_screenshot
from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__, "application")


def collect_simple_tcode_screenshots(tcodes: list[str], wait_seconds: float = 2.0) -> list[MetricResult]:
    results = []

    try:
        session = get_scripting_session()
    except Exception as e:
        log.error(f"Could not acquire scripting session: {e}")
        return results

    for tcode in tcodes:
        try:
            verified = open_tcode(session, tcode, wait_seconds=wait_seconds)
            screenshot_path = capture_screenshot(session, tcode)
            results.append(MetricResult(
                name=f"screenshot_{tcode}",
                value=None,
                display_value="captured" if screenshot_path else "failed",
                status=Status.UNKNOWN,
                source="sap_gui_collector",
                tcode=tcode,
                detail=f"Evidence screenshot for {tcode} (verified={verified})",
                screenshot_path=screenshot_path or None,
            ))
        except Exception as e:
            log.error(f"Failed to collect T-code {tcode}: {e}")
            results.append(MetricResult(
                name=f"screenshot_{tcode}",
                value=None,
                display_value="failed",
                status=Status.UNKNOWN,
                source="sap_gui_collector",
                tcode=tcode,
                detail=f"Error: {e}",
            ))

    log.info(f"Collected {len(results)} T-code screenshots.")
    return results
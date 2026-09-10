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
from core import heartbeat

log = get_logger(__name__, "application")


def _recompress(filepath: str) -> None:
    """
    Re-encode a hardCopy PNG in place, LOSSLESSLY, at roughly 1/100th its size.

    SAP GUI's hardCopy writes an essentially uncompressed PNG: measured on this
    project's own evidence, a 1920x1008 screen is 5,806,134 bytes. The identical
    pixels re-encoded by Pillow with optimize=True are 46-138KB depending on
    screen content -- a 41x to 123x reduction with the image bit-for-bit
    unchanged.

    That bloat was being paid four separate times per screenshot: writing 5.8MB
    to disk, the file-ready poll below waiting for that write to flush,
    Tesseract decoding it during OCR, and pdflatex reading it while building the
    report. Four days of evidence occupied 944MB; re-encoded it is 6.5MB.

    WHY NOT A PALETTE.
    Quantising to a 64- or 256-colour palette is smaller still (16-59KB), and it
    is tempting because SAP GUI looks like flat colour. It is not: the text is
    subpixel-antialiased, so quantisation moves the greys around the glyph
    edges. Measured against the originals, OCR output matched only 69-96% with a
    64-colour palette and 79-94% at 256, while lossless matched 100.00% on every
    screen tested. Evidence that reads differently after compression is not
    evidence. The extra 30KB is not worth an OCR-derived lock count being wrong.

    Best-effort by design. A recompression failure leaves the original file in
    place and the pipeline continues -- evidence that is large is still
    evidence, and this must never be the reason a sweep fails.
    """
    try:
        from PIL import Image
    except Exception as e:  # noqa: BLE001 -- Pillow missing is not fatal here
        log.warning(f"Pillow unavailable, leaving screenshot uncompressed: {e}")
        return

    try:
        before = os.path.getsize(filepath)
        with Image.open(filepath) as img:
            img.load()
            img.save(filepath, "PNG", optimize=True)
        after = os.path.getsize(filepath)
        log.debug(
            f"Recompressed {os.path.basename(filepath)}: "
            f"{before // 1024}KB -> {after // 1024}KB"
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not recompress {filepath}, keeping original: {e}")


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
                    # A non-zero size is not the same as a finished write:
                    # hardCopy streams several MB, so a poll can catch the file
                    # mid-flush. Require the size to hold steady across two
                    # polls before treating it as complete, otherwise
                    # _recompress() below can open a truncated PNG.
                    size = os.path.getsize(filepath)
                    if size > 0:
                        time.sleep(0.1)
                        if os.path.getsize(filepath) == size:
                            _recompress(filepath)
                            heartbeat.beat(f"screenshot:{tcode}")
                            log.info(f"Screenshot saved: {filepath}")
                            return filepath
                        continue
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
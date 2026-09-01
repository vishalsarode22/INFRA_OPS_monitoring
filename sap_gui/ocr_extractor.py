"""
OCR-based value extraction from SAP GUI screenshots, using Tesseract.
Chosen over an AI vision API for three reasons: free with no rate limits,
fully offline (SAP screenshots never leave the machine -- important for
enterprise data policy), and deterministic (no hallucination risk on
numeric values like lock counts).

Screenshots are preprocessed (grayscale, upscaled, autocontrast) before
OCR, since SAP's compressed grid fonts (e.g. SM51, ST03N) are small
enough that Tesseract misreads them at native resolution.
"""

import os
import re
import pytesseract
from PIL import Image, ImageOps
from dotenv import load_dotenv

load_dotenv()

from utils.logger import get_logger

log = get_logger(__name__, "application")

_configured = False

_COMMON_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
]


def _configure_tesseract():
    """
    Configures both the Tesseract executable path AND TESSDATA_PREFIX
    (the language-data directory). Setting only the executable path
    (as this used to do) is NOT enough -- Tesseract also needs to know
    where its .traineddata files live, and without TESSDATA_PREFIX set
    it fails with "Error opening data file ... Could not initialize
    tesseract." even when tesseract.exe itself runs fine.

    Resolution order:
      1. .env TESSERACT_PATH / TESSDATA_PREFIX, if set explicitly.
      2. Auto-detect from common Windows install locations.
      3. Fall back to whatever's already on PATH (previous behavior).
    """
    global _configured
    if _configured:
        return

    exe_path = os.getenv("TESSERACT_PATH")
    if not exe_path or not os.path.exists(exe_path):
        exe_path = next((p for p in _COMMON_TESSERACT_PATHS if os.path.exists(p)), None)

    if exe_path:
        pytesseract.pytesseract.tesseract_cmd = exe_path

    tessdata_dir = os.getenv("TESSDATA_PREFIX")
    if not tessdata_dir or not os.path.isdir(tessdata_dir):
        # Tesseract's data dir is normally <install_dir>\tessdata
        candidates = []
        if exe_path:
            candidates.append(os.path.join(os.path.dirname(exe_path), "tessdata"))
        candidates += [os.path.join(os.path.dirname(p), "tessdata") for p in _COMMON_TESSERACT_PATHS]
        tessdata_dir = next((d for d in candidates if os.path.isdir(d)), None)

    if tessdata_dir:
        os.environ["TESSDATA_PREFIX"] = tessdata_dir
    else:
        log.warning(
            "Could not locate a tessdata directory (checked .env TESSDATA_PREFIX and "
            "common Tesseract install locations). OCR calls will likely fail until "
            "TESSDATA_PREFIX is set in .env to your tessdata folder."
        )

    _configured = True


def _preprocess_for_ocr(image_path: str) -> Image.Image:
    """
    Prepare SAP GUI screenshots for OCR.

    Large SAP GUI screenshots can consume excessive memory when
    upscaled 2x. Keep normal screenshots at native resolution and
    only upscale smaller images where it is useful.
    """
    img = Image.open(image_path).convert("L")

    max_dimension = 3000
    max_pixels = 12_000_000

    width, height = img.size

    # Downscale unusually large screenshots before OCR.
    scale = min(
        1.0,
        max_dimension / max(width, height),
        (max_pixels / (width * height)) ** 0.5,
    )

    if scale < 1.0:
        new_size = (
            max(1, int(width * scale)),
            max(1, int(height * scale)),
        )
        img = img.resize(new_size, Image.LANCZOS)

    # Only upscale relatively small screenshots.
    elif max(width, height) < 1800:
        img = img.resize(
            (width * 2, height * 2),
            Image.LANCZOS,
        )

    img = ImageOps.autocontrast(img)
    return img


def run_ocr(image_path: str) -> str:
    """Returns the full OCR text extracted from a screenshot, after preprocessing."""
    _configure_tesseract()
    try:
        img = _preprocess_for_ocr(image_path)
        text = pytesseract.image_to_string(img)
        return text
    except Exception as e:
        log.error(f"OCR failed for {image_path}: {e}")
        return ""


def extract_patterns(text: str, patterns: dict) -> dict:
    """
    patterns: {result_key: regex_with_one_capture_group}
    Returns {result_key: matched_value} for every pattern that matched.
    """
    results = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            results[key] = match.group(1).strip()
    return results


def count_date_prefixed_lines(text: str, date_pattern: str = r"^\d{2}\.\d{2}\.\d{4}\b") -> int:
    """
    Counts lines that start with a date (dd.mm.yyyy), used for ST22-style
    screens where each dump/entry is its own row starting with a date,
    and no reliable single 'total' line exists elsewhere on screen.
    """
    count = 0
    for line in text.splitlines():
        if re.match(date_pattern, line.strip()):
            count += 1
    return count


def count_time_prefixed_rows(text: str, pattern: str = r"\d{2}:\d{2}:\d{2}") -> int:
    """Counts lines containing a HH:MM:SS time, used for job/queue list rows."""
    count = 0
    for line in text.splitlines():
        if re.search(pattern, line):
            count += 1
    return count


def count_spool_rows(text: str) -> int:
    """
    Counts visible SP01 spool request rows (lines starting with a spool number).
    NOTE: only counts rows visible in the captured screenshot -- if the real
    list is longer than one screen, this undercounts. A true total would need
    reading the grid's row count directly via scripting, which we haven't
    found a working control ID for yet on this SAP version.
    """
    count = 0
    for line in text.splitlines():
        if re.match(r"^\d{4,6}\s", line.strip()):
            count += 1
    return count


def count_occurrences(text: str, keyword: str) -> int:
    """Case-insensitive count of a keyword's occurrences across all lines."""
    return len(re.findall(re.escape(keyword), text, re.IGNORECASE))


def debug_ocr_dump(image_path: str):
    """Prints the full raw OCR text for a screenshot -- use to build new patterns."""
    text = run_ocr(image_path)
    print(f"\n{'='*60}\nOCR TEXT: {image_path}\n{'='*60}")
    print(text)
    print("=" * 60)
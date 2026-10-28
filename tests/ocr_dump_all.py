import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sap_gui.ocr_extractor import debug_ocr_dump

if __name__ == "__main__":
    today = sys.argv[1] if len(sys.argv) > 1 else None
    if not today:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

    folder = f"reports/{today}/screenshots"
    tcode_filter = sys.argv[2] if len(sys.argv) > 2 else None

    files = sorted(glob.glob(f"{folder}/*.png"))
    if tcode_filter:
        files = [f for f in files if tcode_filter.upper() in os.path.basename(f).upper()]

    for f in files:
        debug_ocr_dump(f)
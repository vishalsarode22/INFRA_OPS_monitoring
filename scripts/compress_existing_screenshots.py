"""
One-off backfill: recompress screenshots already written by SAP GUI hardCopy.

sap_gui/screenshot.py now recompresses every new capture, but the evidence
already on disk was written by the old path. Measured on this project's
reports/ folder: 170 screenshots occupying 990MB, where the same images
re-encoded occupy roughly 8MB.

The re-encode is LOSSLESS: pixels are unchanged, so OCR run against a
recompressed screenshot returns byte-identical text (verified across SM12,
SM37, SM50 and SM51 captures). Palette quantisation would be smaller again but
degrades OCR -- see the note in sap_gui/screenshot.py._recompress.

Safe to run repeatedly -- a file already small enough is skipped, so a second
run is a no-op. Safe to run while the dashboard is up; it only touches PNGs
under reports/.

    python scripts\\compress_existing_screenshots.py --dry-run
    python scripts\\compress_existing_screenshots.py
    python scripts\\compress_existing_screenshots.py --dir reports/2026-09-10

The *_latex.png duplicates created by the old _sanitize_image_for_latex() are
deleted by default: they are regenerable copies of the originals, and the
report builder no longer needs them. Keep them with --keep-latex-copies.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from utils.paths import BASE_DIR

# Anything already under this is either recompressed or genuinely small.
SKIP_BELOW_BYTES = 400_000


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def compress(path: str, dry_run: bool) -> tuple[int, int]:
    """Returns (bytes_before, bytes_after). after == before means skipped."""
    before = os.path.getsize(path)
    if before < SKIP_BELOW_BYTES:
        return before, before
    if dry_run:
        # Estimate without writing, so --dry-run reports a real number.
        import io
        buf = io.BytesIO()
        with Image.open(path) as img:
            img.load()
            img.save(buf, "PNG", optimize=True)
        return before, buf.tell()

    tmp = path + ".tmp"
    try:
        with Image.open(path) as img:
            img.load()
            img.save(tmp, "PNG", optimize=True)
        after = os.path.getsize(tmp)
        # Never replace a file with a larger one.
        if after >= before:
            os.remove(tmp)
            return before, before
        os.replace(tmp, path)
        return before, after
    except Exception as e:  # noqa: BLE001
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"  !! {path}: {type(e).__name__}: {e}")
        return before, before


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(BASE_DIR, "reports"),
                    help="root to walk (default: reports/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the saving without writing anything")
    ap.add_argument("--keep-latex-copies", action="store_true",
                    help="keep the *_latex.png duplicates instead of deleting them")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"Not a directory: {args.dir}")
        return 1

    total_before = total_after = 0
    changed = skipped = removed = 0
    removed_bytes = 0

    for root, _dirs, files in os.walk(args.dir):
        for fn in files:
            if not fn.lower().endswith(".png"):
                continue
            path = os.path.join(root, fn)

            if fn.endswith("_latex.png") and not args.keep_latex_copies:
                size = os.path.getsize(path)
                if not args.dry_run:
                    try:
                        os.remove(path)
                    except OSError as e:
                        print(f"  !! could not remove {path}: {e}")
                        continue
                removed += 1
                removed_bytes += size
                continue

            before, after = compress(path, args.dry_run)
            total_before += before
            total_after += after
            if after < before:
                changed += 1
            else:
                skipped += 1

    verb = "would save" if args.dry_run else "saved"
    print()
    print(f"  screenshots recompressed : {changed}")
    print(f"  screenshots skipped      : {skipped}")
    print(f"  _latex.png duplicates    : {removed} ({_human(removed_bytes)})")
    print(f"  before                   : {_human(total_before)}")
    print(f"  after                    : {_human(total_after)}")
    print(f"  {verb:24} : {_human(total_before - total_after + removed_bytes)}")
    if args.dry_run:
        print("\n  (dry run -- nothing was written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

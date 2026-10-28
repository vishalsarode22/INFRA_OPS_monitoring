"""
Switch automatic monitoring on or off in .env -- changes ONLY the
SCHEDULER_ENABLED lines, nothing else.

    .venv\\Scripts\\python.exe scripts\\set_scheduler_enabled.py --check
    .venv\\Scripts\\python.exe scripts\\set_scheduler_enabled.py true
    .venv\\Scripts\\python.exe scripts\\set_scheduler_enabled.py false

.env may contain SCHEDULER_ENABLED more than once; the LAST one wins when the
dashboard starts. This script sets every occurrence to the same value so the
file cannot contradict itself. A copy of .env is kept first
(.env.bak-<date>-<time>). Line endings, encoding and every other byte stay
as they are. No value from .env is printed.

The change takes effect when the dashboard restarts.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
LINE = re.compile(rb"^(SCHEDULER_ENABLED[ \t]*=[ \t]*)([^\r\n]*)(\r?)$", re.M)


def current(data: bytes) -> list[str]:
    return [m.group(2).decode("ascii", "replace").strip() for m in LINE.finditer(data)]


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("--check", "true", "false"):
        print(__doc__)
        return 2
    if not os.path.isfile(ENV):
        print(f"No .env at {ENV}")
        return 1
    with open(ENV, "rb") as fh:
        data = fh.read()
    values = current(data)
    effective = values[-1] if values else "(not set: on by default)"
    print(f"SCHEDULER_ENABLED lines: {values or 'none'} -> in effect: {effective}")
    if argv[0] == "--check":
        return 0

    want = argv[0].encode()
    if not values:
        nl = b"\r\n" if b"\r\n" in data else b"\n"
        new = data + (b"" if data.endswith((b"\n", b"\r")) or not data else nl) \
            + b"SCHEDULER_ENABLED=" + want + nl
    else:
        new = LINE.sub(lambda m: m.group(1) + want + m.group(3), data)
    if new == data:
        print("Already set; nothing changed.")
        return 0

    backup = f"{ENV}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(ENV, backup)
    tmp = ENV + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(new)
    os.replace(tmp, ENV)

    # Verify: only the SCHEDULER_ENABLED lines differ.
    old_lines, new_lines = data.split(b"\n"), new.split(b"\n")
    changed = [i + 1 for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    extra = len(new_lines) - len(old_lines)
    print(f"Backup: {os.path.basename(backup)}")
    print(f"Changed line(s): {changed}{' + 1 added' if extra else ''}")
    print(f"Now: {current(new)} -> in effect: {current(new)[-1]}")
    print("Restart the dashboard for this to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
Change monitoring switches in .env -- ONLY the settings listed below, nothing
else, and no value from .env is ever printed except these switches.

    .venv\\Scripts\\python.exe scripts\\set_env.py --check
    .venv\\Scripts\\python.exe scripts\\set_env.py live-check-on        # every 5 minutes
    .venv\\Scripts\\python.exe scripts\\set_env.py live-check-on 10     # every 10 minutes
    .venv\\Scripts\\python.exe scripts\\set_env.py live-check-off

live-check-on   IBO_LIVE_AUTO_REFRESH=1 and IBO_LIVE_REFRESH_SECONDS=<minutes*60>:
                the dashboard reads every system over RFC in the background
                (no SAP GUI), grades it with the Thresholds limits and feeds
                the automatic RCA. One RFC logon per system per interval.
live-check-off  IBO_LIVE_AUTO_REFRESH=0: systems are read only when someone
                clicks "Read live" on the wall.

A setting that appears more than once is set the same everywhere (the LAST
one wins when the dashboard starts). A copy of .env is kept first
(.env.bak-<date>-<time>). Line endings, encoding and every other byte stay as
they are. Takes effect when the dashboard restarts.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(ROOT, ".env")
ALLOWED = ("IBO_LIVE_AUTO_REFRESH", "IBO_LIVE_REFRESH_SECONDS", "SCHEDULER_ENABLED")


def _line(name: str):
    return re.compile(rb"^(" + name.encode() + rb"[ \t]*=[ \t]*)([^\r\n]*)(\r?)$", re.M)


def values(data: bytes, name: str) -> list[str]:
    return [m.group(2).decode("ascii", "replace").strip() for m in _line(name).finditer(data)]


def set_value(data: bytes, name: str, value: str) -> bytes:
    if name not in ALLOWED:
        raise ValueError(f"{name} is not a setting this script may change")
    if values(data, name):
        return _line(name).sub(lambda m: m.group(1) + value.encode() + m.group(3), data)
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    if data and not data.endswith((b"\n", b"\r")):
        data += nl
    return data + name.encode() + b"=" + value.encode() + nl


def show(data: bytes) -> None:
    for name in ALLOWED:
        v = values(data, name)
        print(f"  {name:<26} {v if v else '(not set)'}" + (f"  -> in effect: {v[-1]}" if v else ""))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("--check", "live-check-on", "live-check-off"):
        print(__doc__)
        return 2
    if not os.path.isfile(ENV):
        print(f"No .env at {ENV}")
        return 1
    with open(ENV, "rb") as fh:
        data = fh.read()
    print("Now:")
    show(data)
    if argv[0] == "--check":
        return 0
    new = data
    if argv[0] == "live-check-on":
        try:
            minutes = int(argv[1]) if len(argv) > 1 else 5
        except ValueError:
            print("Minutes must be a whole number, e.g. 5")
            return 2
        if not 1 <= minutes <= 60:
            print("Choose between 1 and 60 minutes.")
            return 2
        new = set_value(new, "IBO_LIVE_AUTO_REFRESH", "1")
        new = set_value(new, "IBO_LIVE_REFRESH_SECONDS", str(minutes * 60))
    else:
        new = set_value(new, "IBO_LIVE_AUTO_REFRESH", "0")
    if new == data:
        print("Already set; nothing changed.")
        return 0
    backup = f"{ENV}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(ENV, backup)
    tmp = ENV + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(new)
    os.replace(tmp, ENV)
    old_lines, new_lines = data.split(b"\n"), new.split(b"\n")
    changed = [i + 1 for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    added = len(new_lines) - len(old_lines)
    print(f"Backup: {os.path.basename(backup)}")
    print(f"Changed line(s): {changed}" + (f" + {added} added at the end" if added else ""))
    print("After:")
    show(new)
    print("Restart the dashboard for this to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

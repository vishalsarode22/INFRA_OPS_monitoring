"""
Move unwanted files (old .bak copies, scratch check_*.py scripts, patch files,
old backup folders) into _to_delete\\<date>\\ -- nothing is deleted.

    .venv\\Scripts\\python.exe scripts\\cleanup_move_aside.py            (show what would move)
    .venv\\Scripts\\python.exe scripts\\cleanup_move_aside.py --apply    (move them)
    .venv\\Scripts\\python.exe scripts\\cleanup_move_aside.py --undo _to_delete\\20261029-101500

Only files on the list below (and matching patterns) are touched. .env backups
are moved to _to_delete\\...\\secrets\\ and listed separately: they hold
passwords, so empty that folder yourself once you are sure.
Stop the dashboard first (some .bak files sit next to running code).
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exact paths (relative to the IBOPS folder).
FILES = [
    "main.py.bak", "dashboard/app.py.bak3",
    "dashboard/static/wall.html.bak", "dashboard/static/wall.html.bak2",
    "dashboard/static/wall.html.bak3", "dashboard/static/wall.html.bak4",
    "reporting/production_reports.py.bak", "reporting/production_reports.py.bak2",
    "reporting/excel_template_writer.py.bak",
    "packaging/windows/ibops_dashboard_supervisor.ps1.bak",
    "packaging/windows/install_keep_awake.ps1.bak-20261009",
    "ibops-remote-run.patch", "ibops-remote-sources.patch", "infrabeatops_perf_fixes.patch",
    "test_apparel_rfc.py", "watch_stat.py", "apply_linux_portability.py", "inventory_windows.json",
]
# Whole folders.
FOLDERS = ["backup-sched-20261009", "backup-stage1-20261009"]
# Patterns, matched against paths relative to the IBOPS folder.
PATTERNS = ["check_*.py", "collectors/*.py.bak*", "config/*.bak*", "config/*.bak-*"]
# .env backups: secrets -- moved into their own subfolder.
SECRET_PATTERNS = [".env.bak*", ".env.*.bak"]
NEVER = {".env", ".env.example", "config/systems.yaml", "config/schedules.json"}


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) for p in patterns)


def candidates() -> list[tuple[str, str]]:
    """[(relative path, 'file'|'folder'|'secret')]"""
    out = []
    for rel in FILES:
        if os.path.isfile(os.path.join(ROOT, rel)):
            out.append((rel, "file"))
    for rel in FOLDERS:
        if os.path.isdir(os.path.join(ROOT, rel)):
            out.append((rel, "folder"))
    for dirpath, dirnames, filenames in os.walk(ROOT):
        reldir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        dirnames[:] = [d for d in dirnames if d not in (".git", ".venv", "_to_delete", "node_modules")]
        for fn in filenames:
            rel = fn if reldir == "." else f"{reldir}/{fn}"
            if rel in NEVER or any(rel == c[0] for c in out):
                continue
            if reldir == "." and _matches(fn, SECRET_PATTERNS):
                out.append((rel, "secret"))
            elif _matches(rel, PATTERNS):
                out.append((rel, "file"))
    return sorted(out)


def _size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def apply(items, dest) -> None:
    moved = []
    for rel, kind in items:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(dest, "secrets" if kind == "secret" else "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved.append({"from": rel, "to": os.path.relpath(dst, ROOT), "kind": kind})
    json.dump(moved, open(os.path.join(dest, "moved.json"), "w", encoding="utf-8"), indent=1)
    print(f"Moved {len(moved)} item(s) to {os.path.relpath(dest, ROOT)}. Undo: --undo {os.path.relpath(dest, ROOT)}")


def undo(dest) -> int:
    dest = os.path.join(ROOT, dest) if not os.path.isabs(dest) else dest
    moved = json.load(open(os.path.join(dest, "moved.json"), encoding="utf-8"))
    for m in moved:
        src, back = os.path.join(ROOT, m["to"]), os.path.join(ROOT, m["from"])
        if os.path.exists(back):
            print(f"   {m['from']}: exists again, left in place")
            continue
        os.makedirs(os.path.dirname(back) or ROOT, exist_ok=True)
        shutil.move(src, back)
    print(f"Put back {len(moved)} item(s).")
    return 0


def main(argv) -> int:
    if "--undo" in argv:
        return undo(argv[argv.index("--undo") + 1])
    items = candidates()
    total = 0
    for rel, kind in items:
        size = _size(os.path.join(ROOT, rel))
        total += size
        print(f"   {kind:<7} {size / 1024:>9.0f} KB  {rel}")
    print(f"{len(items)} item(s), {total / 1048576:.1f} MB")
    if "--apply" not in argv:
        print("Nothing moved (add --apply).")
        return 0
    apply(items, os.path.join(ROOT, "_to_delete", time.strftime("%Y%m%d-%H%M%S")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

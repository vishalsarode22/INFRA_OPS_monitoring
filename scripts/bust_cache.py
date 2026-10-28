#!/usr/bin/env python3
"""
Re-stamp the ?v= version on shared assets after editing them.

Browsers cache /static/shell.js aggressively. Replacing the file without
changing its URL leaves users on the old copy, so a change looks like it did
not work -- which costs more time than the change itself.

Run this after editing shell.js or shell.css:

    python scripts/bust_cache.py
"""
import glob
import hashlib
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "dashboard", "static")


def digest(name: str) -> str:
    path = os.path.join(STATIC, name)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()[:8]


def main() -> int:
    versions = {"shell.js": digest("shell.js"), "shell.css": digest("shell.css")}
    changed = 0

    for path in glob.glob(os.path.join(STATIC, "*.html")):
        text = original = open(path, encoding="utf-8").read()
        for asset, version in versions.items():
            if not version:
                continue
            pattern = r"/static/" + asset.replace(".", r"\.") + r"(\?v=[a-f0-9]+)?"
            text = re.sub(pattern, f"/static/{asset}?v={version}", text)
        if text != original:
            open(path, "w", encoding="utf-8").write(text)
            print(f"  updated {os.path.basename(path)}")
            changed += 1

    print(f"\n{changed} page(s) re-stamped. "
          f"shell.js v={versions['shell.js']} shell.css v={versions['shell.css']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

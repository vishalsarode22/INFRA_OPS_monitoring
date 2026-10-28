"""
Put systems into projects (the `customer:` of a system = its project on the
Team & access page). A system without one shows as "Infrabeat".

    .venv\\Scripts\\python.exe scripts\\set_projects.py --list
    .venv\\Scripts\\python.exe scripts\\set_projects.py "CEAT*=CEAT" "CARFOUR*=AG" "CAR PRD=CAR" "PS4=AG" --dry-run
    .venv\\Scripts\\python.exe scripts\\set_projects.py "CEAT*=CEAT" "CARFOUR*=AG" "CAR PRD=CAR" "PS4=AG"

Each rule is  <system name or pattern with *>=<project>. Backups:
config\\systems.yaml.bak-pause-<time>. Nothing is written unless the result is
valid YAML with the same systems. The dashboard picks it up within seconds.
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pause_system as P  # noqa: E402

_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,39}$")


def _set_customer(block: list[str], project: str, nl: str) -> list[str]:
    name_i = next(i for i, ln in enumerate(block) if P._name_of(ln) is not None)
    ind = " " * (len(block[name_i]) - len(block[name_i].lstrip(" ")) + 2)
    out = [ln for ln in block if not re.match(rf"^{ind}customer\s*:", ln)]
    at = next(i for i, ln in enumerate(out) if P._name_of(ln) is not None) + 1
    return out[:at] + [f"{ind}customer: {project}{nl}"] + out[at:]


def main(argv: list[str]) -> int:
    text = P._read(P.LIVE)
    if not text:
        print("config\\systems.yaml not found -- run this from the IBOPS folder.")
        return 1
    import yaml
    data = yaml.safe_load(text) or {}
    current = {str(s.get("name")): str(s.get("customer") or "") for s in data.get("systems") or []}
    if not argv or "--list" in argv:
        for n, c in current.items():
            print(f"   {n:<28} {c or '(none -> Infrabeat)'}")
        return 0
    rules = []
    for a in argv:
        if a.startswith("--"):
            continue
        pat, _, proj = a.partition("=")
        if not pat.strip() or not _PROJECT.match(proj.strip()):
            print(f"Not a rule: {a!r}  (use  NAME=PROJECT  or  PATTERN*=PROJECT)")
            return 2
        rules.append((pat.strip(), proj.strip()))
    nl = "\r\n" if "\r\n" in text else "\n"
    head, blks, tail = P.blocks(text)
    names = [n for n, _ in blks]
    out, changes = [], []
    for name, block in blks:
        proj = next((p for pat, p in rules if fnmatch.fnmatchcase(name.upper(), pat.upper())), None)
        if proj and current.get(name) != proj:
            out.append(_set_customer(block, proj, nl))
            changes.append(f"   {name:<28} {current.get(name) or 'Infrabeat'} -> {proj}")
        else:
            out.append(block)
    unmatched = [pat for pat, _ in rules if not any(fnmatch.fnmatchcase(n.upper(), pat.upper()) for n in names)]
    for pat in unmatched:
        print(f"   no system matches {pat!r}")
    if not changes:
        print("Nothing to change.")
        return 0
    new = "".join(head) + "".join("".join(b) for b in out) + "".join(tail)
    P._check(new, names, P.LIVE)
    print("\n".join(changes))
    if "--dry-run" in argv:
        print("DRY RUN -- nothing changed.")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    P._write(P.LIVE, new, stamp)
    print(f"Saved (backup systems.yaml.bak-pause-{stamp}). Give people their projects on the Team & access page.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

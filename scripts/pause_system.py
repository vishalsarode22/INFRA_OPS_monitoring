"""
Pause (or resume) monitoring of SAP systems WITHOUT deleting anything.

    pause:   python scripts/pause_system.py SARLOHA_QAS "SARLOHA PRD"
    resume:  python scripts/pause_system.py --resume SARLOHA_QAS "SARLOHA PRD"
    list:    python scripts/pause_system.py --list

Pause moves each system's block (with the comment lines right above it)
from config/systems.yaml to config/systems_paused.yaml, exactly as written
-- nothing is re-formatted, the .env passwords stay where they are. Its
lines in config/tunnel_forwards.txt (the ones whose comment names the
system) are commented out with "#PAUSED# ". Resume puts both back.

The dashboard notices the change within seconds (systems.yaml is re-read
when it changes): the system leaves the wall, no live reads, no sweeps, no
alerts. History stays. A backup of every changed file is kept
(<file>.bak-pause-<date-time>). Nothing is written unless the result is
valid YAML with exactly the expected systems.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
LIVE = os.path.join(CONFIG, "systems.yaml")
PAUSED = os.path.join(CONFIG, "systems_paused.yaml")
FORWARDS = os.path.join(CONFIG, "tunnel_forwards.txt")
MARK = "#PAUSED# "
PAUSED_HEADER = ("# Systems taken out of monitoring with scripts/pause_system.py.\n"
                 "# This file is NOT read by the dashboard. To monitor one again:\n"
                 "#   python scripts/pause_system.py --resume <NAME>\n"
                 "systems:\n")


def _read(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: str, text: str, stamp: str) -> None:
    if os.path.isfile(path):
        shutil.copy2(path, f"{path}.bak-pause-{stamp}")
    tmp = path + ".tmp-pause"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _name_of(line: str) -> str | None:
    m = re.match(r"^\s*-\s+name:\s*(.*?)\s*$", line.rstrip("\r\n"))
    if not m:
        return None
    v = m.group(1)
    if v[:1] in "'\"" and v[-1:] == v[:1]:
        v = v[1:-1]
    return v.split(" #")[0].strip()


def blocks(text: str) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    """(head lines, [(name, block lines)], tail lines) of a `systems:` list file."""
    lines = text.splitlines(keepends=True)
    items = [i for i, ln in enumerate(lines) if _name_of(ln) is not None]
    if not items:
        return lines, [], []
    indent = len(lines[items[0]]) - len(lines[items[0]].lstrip(" "))
    starts = []
    for i in items:
        if len(lines[i]) - len(lines[i].lstrip(" ")) != indent:
            continue
        s = i
        while s > 0 and lines[s - 1].strip().startswith("#") and \
                (len(lines[s - 1]) - len(lines[s - 1].lstrip(" "))) <= indent:
            s -= 1                                   # comment lines right above belong to it
        starts.append((s, i))
    head = lines[:starts[0][0]]
    out = []
    last_end = len(lines)
    for n, (s, i) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        if n + 1 == len(starts):                     # last block: stop at the next top-level key
            for j in range(i + 1, len(lines)):
                raw = lines[j]
                if raw.strip() and raw[0] not in " \t#" and not (indent == 0 and raw.startswith("-")):
                    end = j
                    break
            last_end = end
        out.append((_name_of(lines[i]), lines[s:end]))
    return head, out, lines[last_end:]


def _check(text: str, expect: list[str], path: str) -> None:
    import yaml
    data = yaml.safe_load(text) or {}
    got = [str(s.get("name")) for s in (data.get("systems") or []) if isinstance(s, dict)]
    if sorted(got) != sorted(expect):
        raise SystemExit(f"STOP: {os.path.basename(path)} would not hold the expected systems "
                         f"(expected {expect}, got {got}). Nothing was changed.")


def _forwards(names: list[str], pause: bool, stamp: str) -> list[str]:
    text = _read(FORWARDS)
    if not text:
        return []
    changed, out = [], []
    for ln in text.splitlines(keepends=True):
        body = ln[len(MARK):] if ln.startswith(MARK) else ln
        comment = body.split("#", 1)[1] if "#" in body else ""
        hit = any(re.search(r"(^|\s)" + re.escape(n) + r"(\s|$)", comment, re.I) for n in names)
        if hit and pause and not ln.startswith(MARK) and not ln.lstrip().startswith("#"):
            ln = MARK + ln
            changed.append(body.strip())
        elif hit and not pause and ln.startswith(MARK):
            ln = body
            changed.append(body.strip())
        out.append(ln)
    if changed:
        _write(FORWARDS, "".join(out), stamp)
    return changed


def move(names: list[str], resume: bool) -> int:
    src, dst = (PAUSED, LIVE) if resume else (LIVE, PAUSED)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    s_text, d_text = _read(src), _read(dst)
    if not s_text:
        print(f"{os.path.basename(src)} not found or empty -- nothing to do.")
        return 1
    nl = "\r\n" if "\r\n" in s_text else "\n"
    if not d_text.strip():
        d_text = (PAUSED_HEADER if dst == PAUSED else "systems:\n").replace("\n", nl)
    s_head, s_blocks, s_tail = blocks(s_text)
    d_head, d_blocks, d_tail = blocks(d_text)
    s_names = [n for n, _ in s_blocks]
    d_names = [n for n, _ in d_blocks]
    missing = [n for n in names if n not in s_names]
    if missing:
        where = "monitored (systems.yaml)" if not resume else "paused (systems_paused.yaml)"
        print(f"Not {where}: {', '.join(missing)}.  There: {', '.join(s_names) or 'none'}")
        if len(missing) == len(names):
            return 1
    take = [n for n in names if n in s_names]
    clash = [n for n in take if n in d_names]
    if clash:
        print(f"STOP: already in {os.path.basename(dst)}: {', '.join(clash)}. Nothing was changed.")
        return 2
    moved = [b for n, b in s_blocks if n in take]
    keep = [b for n, b in s_blocks if n not in take]
    new_src = "".join(s_head) + "".join("".join(b) for b in keep) + "".join(s_tail)
    if not keep and src == LIVE:
        print("STOP: that would leave systems.yaml with no system at all. Nothing was changed.")
        return 2
    base = "".join(d_head) + "".join("".join(b) for _, b in d_blocks) + "".join(d_tail)
    if base and not base.endswith(("\n", "\r\n")):
        base += nl
    add = ""
    for b in moved:
        chunk = "".join(b)
        if not chunk.endswith(("\n", "\r\n")):
            chunk += nl
        add += chunk
    new_dst = base + add
    _check(new_src, [n for n in s_names if n not in take], src)
    _check(new_dst, d_names + take, dst)
    _write(dst, new_dst, stamp)                     # add first: a crash in between never loses a system
    _write(src, new_src, stamp)
    fw = _forwards(take, pause=not resume, stamp=stamp)
    verb = "Resumed" if resume else "Paused"
    print(f"{verb}: {', '.join(take)}")
    print(f"   {os.path.basename(src)} -> {os.path.basename(dst)} (backups: *.bak-pause-{stamp})")
    for f in fw:
        print(f"   tunnel forward {'commented out' if not resume else 'back on'}: {f}")
    if fw:
        print("   The SSH tunnel uses the new forwards after it reconnects (restart the IBOPS Tunnel task).")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "--list":
        print("monitored:", ", ".join(n for n, _ in blocks(_read(LIVE))[1]) or "none")
        print("paused:   ", ", ".join(n for n, _ in blocks(_read(PAUSED))[1]) or "none")
        return 0
    resume = argv[0] == "--resume"
    names = argv[1:] if resume else argv
    if not names:
        print("Name at least one system.")
        return 2
    return move(names, resume)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

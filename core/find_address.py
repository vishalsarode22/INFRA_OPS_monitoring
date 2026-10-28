"""
Where is this e-mail address configured?

    .venv\\Scripts\\python.exe -m core.find_address staff.c@infrabeat.com

Scans everything that can put an address on a mail: config/*.yaml|json
(systems, Alert teams, on-call, thresholds, profiles, schedules...), the
documents IBOPS keeps in its database (the store backend), the dashboard
users, and .env (only the setting NAMES are printed, never values). Prints
one line per place, so a person removed from one list but still mailed
can be removed from the list that really sends to them.

IBO_MAIL_EXCLUDE=a@x.com,b@x.com in .env removes addresses from EVERY mail
IBOPS sends, whatever list they are in (notifications/mailer.py) -- the
safety net for someone who left the team.
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which screen edits which file -- so the answer says where to click.
WHERE = {
    "systems.yaml": "Systems page > Edit the system (Basis team / manager / Report To / Cc / own people)",
    "alert_teams.yaml": "Thresholds & alerts page > Alert teams (a system's own Basis team here replaces its Systems-page Basis box; the standard team is used only when that box is empty)",
    "oncall.yaml": "Operations page > On-call rotation (used when a system's Basis box is empty)",
    "thresholds.yaml": "Thresholds & alerts page (teams of a value)",
    "thresholds_systems.yaml": "Thresholds & alerts page, per system (teams of a value)",
    "profiles.yaml": "Profiles page (Report To / Report Cc of a profile)",
    "rca.yaml": "config/rca.yaml deliver.recipients (RCA mails)",
    "delivery_accounts.yaml": "Settings > Delivery accounts",
}


def _lines_with(text: str, needle: str) -> list[tuple[int, str]]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        if needle in line.lower():
            out.append((n, line.strip()[:160]))
    return out


def find(address: str, root: str = ROOT) -> list[dict]:
    needle = address.strip().lower()
    local = needle.split("@")[0]
    hits: list[dict] = []
    cfg_dir = os.path.join(root, "config")
    for dirpath, _dirs, files in os.walk(cfg_dir):
        if "backups" in dirpath.replace("\\", "/").split("/"):
            continue
        for f in files:
            if not f.lower().endswith((".yaml", ".yml", ".json")):
                continue
            path = os.path.join(dirpath, f)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for n, line in _lines_with(text, needle):
                hits.append({"where": os.path.relpath(path, root), "line": n, "text": line,
                             "edit": WHERE.get(f, "")})
    # .env: setting names only (values may hold secrets)
    env = os.path.join(root, ".env")
    try:
        for n, line in enumerate(open(env, encoding="utf-8", errors="replace"), 1):
            if needle in line.lower() and "=" in line and not line.lstrip().startswith("#"):
                hits.append({"where": ".env", "line": n, "text": line.split("=", 1)[0].strip() + "=...",
                             "edit": ".env (Notepad), then restart the dashboard"})
    except OSError:
        pass
    # documents kept in the state database (store backend) and dashboard users
    try:
        sys.path.insert(0, root)
        from core import store
        c = store.conn()
        for r in c.execute("SELECT path, body FROM documents").fetchall():
            data = r["body"]
            if needle in str(data).lower():
                name = os.path.basename(str(r["path"]))
                hits.append({"where": f"database document {r['path']}", "line": 0,
                             "text": "(stored in the IBOPS database)", "edit": WHERE.get(name, "")})
        for r in c.execute("SELECT username FROM users").fetchall():
            if needle == str(r["username"]).lower():
                hits.append({"where": "dashboard login", "line": 0, "text": r["username"],
                             "edit": "Team & access page (a login does not receive alert mails)"})
    except Exception:  # noqa: BLE001 -- the file scan above is the main answer
        pass
    if not hits and local:
        # A partial name helps when the address was typed differently.
        for dirpath, _dirs, files in os.walk(cfg_dir):
            for f in files:
                if f.lower().endswith((".yaml", ".yml", ".json")):
                    path = os.path.join(dirpath, f)
                    try:
                        text = open(path, encoding="utf-8", errors="replace").read()
                    except OSError:
                        continue
                    for n, line in _lines_with(text, local):
                        hits.append({"where": os.path.relpath(path, root), "line": n, "text": line,
                                     "edit": WHERE.get(f, "") + " (name only, not the full address)"})
    return hits


def _edit_line(line: str, old: str, new: str) -> str | None:
    """The line with `old` replaced by `new` (or removed when new is ''); None = drop the line."""
    nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
    body = line[:len(line) - len(nl)] if nl else line
    pat = re.compile(re.escape(old), re.I)
    m = re.match(r"^(\s*-\s*)(['\"]?)" + re.escape(old) + r"\2(\s*(#.*)?)$", body, re.I)
    if m:                                             # a list item of its own
        return None if not new else f"{m.group(1)}{m.group(2)}{new}{m.group(2)}{m.group(3)}{nl}"
    if new:
        return pat.sub(new, body) + nl
    out = re.sub(r"\s*,\s*" + re.escape(old) + r"(?=\s*[,'\"\]\s#]|$)", "", body, flags=re.I)
    out = re.sub(re.escape(old) + r"\s*,\s*", "", out, flags=re.I)
    out = pat.sub("", out)
    return out + nl


def replace(address: str, new: str = "", root: str = ROOT, dry: bool = False) -> list[str]:
    """Replace (or remove) an address in config/*.yaml|yml. Backup per file; YAML checked first."""
    import time
    import yaml
    old = address.strip()
    new = new.strip()
    if new and not EMAIL_RE.match(new):
        raise ValueError(f"not an e-mail address: {new}")
    done = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cfg_dir = os.path.join(root, "config")
    for dirpath, _dirs, files in os.walk(cfg_dir):
        if "backups" in dirpath.replace("\\", "/").split("/"):
            continue
        for f in files:
            if not f.lower().endswith((".yaml", ".yml")):
                continue
            path = os.path.join(dirpath, f)
            text = open(path, encoding="utf-8", newline="").read()
            if old.lower() not in text.lower():
                continue
            lines = text.splitlines(keepends=True)
            out, changed = [], 0
            for ln in lines:
                if old.lower() in ln.lower() and not ln.lstrip().startswith("#"):
                    e = _edit_line(ln, old, new)
                    changed += 1
                    if e is not None:
                        out.append(e)
                else:
                    out.append(ln)
            if not changed:
                continue
            new_text = "".join(out)
            try:
                yaml.safe_load(new_text)
            except Exception as exc:  # noqa: BLE001
                done.append(f"NOT changed {os.path.relpath(path, root)}: the result would not be valid YAML ({exc})")
                continue
            if not dry:
                import shutil
                shutil.copy2(path, f"{path}.bak-addr-{stamp}")
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
            done.append(f"{'would change' if dry else 'changed'} {os.path.relpath(path, root)}: {changed} line(s)"
                        + ("" if dry else f" (backup .bak-addr-{stamp})"))
    return done


EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m core.find_address <e-mail address>\n"
              "       python -m core.find_address <old address> --replace <new address> [--dry-run]\n"
              "       python -m core.find_address <old address> --remove [--dry-run]")
        return 2
    os.chdir(ROOT)
    if "--replace" in argv or "--remove" in argv:
        new = argv[argv.index("--replace") + 1] if "--replace" in argv and argv.index("--replace") + 1 < len(argv) else ""
        if "--replace" in argv and not new:
            print("--replace needs the new address")
            return 2
        lines = replace(argv[0], new, dry="--dry-run" in argv)
        print("\n".join(lines) if lines else f"{argv[0]} is in no config file -- nothing changed.")
        left = [h for h in find(argv[0]) if not h["where"].startswith("config")]
        for h in left:
            print(f"  still in {h['where']}: change it on {h['edit'] or 'that page'}")
        if lines and "--dry-run" not in argv:
            print("The dashboard picks the change up within a minute (no restart needed).")
        return 0
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
    except Exception:  # noqa: BLE001
        pass
    hits = find(argv[0])
    if not hits:
        print(f"{argv[0]} is not in any IBOPS setting. If mails still go to it, a system's Basis box may be "
              f"empty and the on-call rotation or .env IBO_BASIS_EMAILS names a group that contains it.")
        return 0
    print(f"{argv[0]} is configured in {len(hits)} place(s):")
    for h in hits:
        loc = f"{h['where']}" + (f" line {h['line']}" if h["line"] else "")
        print(f"  - {loc}: {h['text']}")
        if h["edit"]:
            print(f"      change it on: {h['edit']}")
    print("\nTo stop all mail to it at once, add it to IBO_MAIL_EXCLUDE in .env and restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
One-time migration: move connection details out of config/systems.yaml
into .env, leaving ${VAR} references behind.

    python scripts\\migrate_systems_to_env.py            # dry run: shows what would change
    python scripts\\migrate_systems_to_env.py --apply    # does it (backs up both files first)

Moved: SAP GUI/RFC usernames, RFC hosts, saprouter strings, SSH host/user,
SMTP host/user, sender and alert addresses, and any password that is still
inline. Kept in systems.yaml: names, client, sysnr, language, flags, logon
pad connection names -- structure that is not sensitive.

Variable names follow the existing convention (<SYSTEM>_RFC_HOST,
<SYSTEM>_RFC_USER, <SYSTEM>_SAP_USERNAME ...). If .env already has a variable
with a DIFFERENT value, that field is skipped and reported; nothing in .env
is ever overwritten by this script.

Values are never printed, only variable names.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SYSTEMS = os.path.join(ROOT, "config", "systems.yaml")
ENV = os.path.join(ROOT, ".env")
REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

TOP_LEVEL = {
    "username": "SAP_USERNAME",
    "password": "SAP_PASSWORD",
    "ssh_host": "SSH_HOST",
    "ssh_username": "SSH_USERNAME",
    "ssh_password": "SSH_PASSWORD",
    "smtp_host": "SMTP_HOST",
    "smtp_username": "SMTP_USERNAME",
    "smtp_password": "SMTP_PASSWORD",
    "sender_email": "SENDER_EMAIL",
    "alert_receivers": "ALERT_RECEIVERS",
    "alert_receivers_cc": "ALERT_RECEIVERS_CC",
}
RFC_BLOCK = {
    "ashost": "RFC_HOST",
    "username": "RFC_USER",
    "password": "RFC_PASSWORD",
    "saprouter": "SAPROUTER",
}


def prefix_for(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(name or "SYS").upper()).strip("_")


def read_env(path: str) -> dict:
    values = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    return values


def plan(systems: list, env: dict):
    moves, conflicts = [], []

    def consider(container, field, var):
        value = container.get(field)
        if value is None or str(value).strip() == "" or REF.match(str(value).strip()):
            return
        value = str(value).strip()
        if var in env and env[var] != value:
            conflicts.append(var)
            return
        moves.append((container, field, var, value))

    for s in systems:
        p = prefix_for(s.get("name"))
        for field, suffix in TOP_LEVEL.items():
            consider(s, field, f"{p}_{suffix}")
        rfc = s.get("rfc") or {}
        for field, suffix in RFC_BLOCK.items():
            consider(rfc, field, f"{p}_{suffix}")
    return moves, conflicts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--systems", default=SYSTEMS)
    ap.add_argument("--env", default=ENV)
    args = ap.parse_args()

    with open(args.systems, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    systems = data.get("systems") or []
    env = read_env(args.env)

    moves, conflicts = plan(systems, env)
    if not moves and not conflicts:
        print("systems.yaml already uses ${VAR} references everywhere. Nothing to do.")
        return 0

    print(f"{len(moves)} value(s) to move into .env:")
    for _, field, var, _ in moves:
        print(f"  {field:<20} -> ${{{var}}}")
    if conflicts:
        print("\nSkipped -- .env already has these with a DIFFERENT value. "
              "Check which is right, fix by hand, then re-run:")
        for var in conflicts:
            print(f"  {var}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(args.systems, f"{args.systems}.bak-{stamp}")
    if os.path.isfile(args.env):
        shutil.copy2(args.env, f"{args.env}.bak-{stamp}")

    new_lines = []
    for container, field, var, value in moves:
        container[field] = "${%s}" % var
        if var not in env:
            new_lines.append(f"{var}={value}")
            env[var] = value

    if new_lines:
        with open(args.env, "a", encoding="utf-8") as fh:
            fh.write(f"\n# moved from config/systems.yaml on {stamp}\n")
            fh.write("\n".join(new_lines) + "\n")

    with open(args.systems, "w", encoding="utf-8") as fh:
        fh.write("# InfraBeatOps system registry.\n"
                 "# Only ${VAR} references for connection details -- values live in .env.\n\n")
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)

    print(f"\nDone. Backups: systems.yaml.bak-{stamp}, .env.bak-{stamp} "
          f"(both gitignored). Restart the dashboard to load the new values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

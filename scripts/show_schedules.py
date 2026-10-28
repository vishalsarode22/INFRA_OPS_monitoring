"""
READ-ONLY: what will run automatically, and when it last ran.

    .venv\\Scripts\\python.exe scripts\\show_schedules.py

Shows:
  * whether automatic monitoring is switched on (SCHEDULER_ENABLED in .env);
  * the schedules saved on the Scheduler page (from the database, where the
    dashboard keeps them; config/schedules.json only if the database has none);
  * when the scheduler last started each system;
  * the enabled profiles (Profiles page) and their last run.

Opens the database read-only and changes nothing. Prints no passwords.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("IBO_DB_PATH") or os.path.join(ROOT, "dashboard", "ibops.sqlite")


def doc(con, rel_path: str):
    if con is not None:
        try:
            row = con.execute("SELECT body FROM documents WHERE path=?", (rel_path,)).fetchone()
            if row:
                return json.loads(row[0]), "database"
        except sqlite3.Error:
            pass
    disk = os.path.join(ROOT, *rel_path.split("/"))
    if os.path.isfile(disk):
        with open(disk, "r", encoding="utf-8") as fh:
            return json.load(fh), "file (not yet in database)"
    return None, "none"


def main() -> int:
    vals = []
    env = os.path.join(ROOT, ".env")
    if os.path.isfile(env):
        with open(env, "rb") as fh:
            vals = [m.group(1).decode("ascii", "replace").strip()
                    for m in re.finditer(rb"^SCHEDULER_ENABLED[ \t]*=[ \t]*([^\r\n]*)", fh.read(), re.M)]
    eff = (vals[-1] if vals else "true (default)")
    on = eff.lower().startswith(("1", "true", "yes", "on"))
    print(f"Automatic monitoring (SCHEDULER_ENABLED): {eff}  -> {'ON' if on else 'OFF: nothing runs on its own'}")

    con = None
    if os.path.isfile(DB):
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    sched, src = doc(con, "config/schedules.json")
    state, _ = doc(con, "config/schedule_state.json")
    last = ((state or {}).get("systems") or {}) if isinstance(state, dict) else {}
    print(f"\nScheduler page (source: {src}):")
    if not sched:
        print("  nothing saved")
    for name, cfg in sorted((sched or {}).items()):
        if str(name).startswith("_") or not isinstance(cfg, dict):
            continue
        if not cfg.get("enabled", True):
            what = "off"
        elif cfg.get("mode") == "daily":
            days = cfg.get("days") or []
            what = f"daily at {cfg.get('at')}" + ("" if len(days) in (0, 7) else " on " + ",".join(days))
        else:
            what = f"every {cfg.get('every')} {'min' if cfg.get('mode') == 'minutes' else 'h'}"
        print(f"  {name:<22} {what:<28} last scheduled run: {last.get(name, '-')}")

    pstate, _ = doc(con, "config/profile_state.json")
    plast = ((pstate or {}).get("profiles") or {}) if isinstance(pstate, dict) else {}
    print("\nProfiles page (enabled only):")
    try:
        import yaml
        with open(os.path.join(ROOT, "config", "monitoring_profiles.yaml"), "r", encoding="utf-8") as fh:
            profiles = (yaml.safe_load(fh) or {}).get("profiles") or []
        shown = 0
        for p in profiles:
            s = p.get("schedule") or {}
            if not s.get("enabled", True):
                continue
            shown += 1
            what = (f"daily at {s.get('at')}" if s.get("mode") == "daily"
                    else f"every {s.get('every')} {'min' if s.get('mode') == 'minutes' else 'h'}")
            print(f"  {p.get('id'):<22} {p.get('system', ''):<14} {what:<18} "
                  f"-> {','.join(p.get('deliver') or []) or 'no destination'}   last run: {plast.get(p.get('id'), '-')}")
        if not shown:
            print("  none enabled")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not read profiles: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

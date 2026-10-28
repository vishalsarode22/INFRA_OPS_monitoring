"""
Check the custom alerts (core/custom_alerts.py) for one system, step by step, and
say in plain words what works and what does not. READ-ONLY: one RFC logon,
the same reads the 5-minute check does (TH_WPINFO, /SDF/GET_DUMP_LOG, TBTCO,
ENQUE_READ2, ARFCSSTATE, TRFCQIN). Nothing is changed in SAP, nothing is mailed.

    .venv\\Scripts\\python.exe scripts\\check_custom_alerts.py "CEAT S4P"

A system whose sweeps are parked after a refused logon is not logged on to
(another refused logon could lock the SAP user); add --force to try anyway.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if not args:
        print(__doc__)
        return 2
    name = args[0]
    from core.config_loader import get_all_systems
    cfg = next((s for s in get_all_systems() if s.get("name") == name), None)
    if cfg is None:
        print(f"[X] No system called '{name}'. Systems: {', '.join(s.get('name', '') for s in get_all_systems())}")
        return 1
    print(f"== Custom alerts check for {name}  ({datetime.now():%d.%m.%Y %H:%M})")

    from core import custom_alerts as CA
    print(f"[{'OK' if CA.enabled() else 'X '}] custom alerts switched on (IBO_CUSTOM_ALERTS)"
          + ("" if CA.enabled() else " -- set IBO_CUSTOM_ALERTS=1 in .env"))
    role = os.getenv("IBO_ROLE", "all")
    print(f"[{'OK' if role == 'all' else '! '}] dashboard role '{role}'"
          + ("" if role == "all" else " -- the 5-minute check runs in the jobs service, not the dashboard"))
    rfc = cfg.get("rfc") or {}
    if not rfc.get("ashost"):
        print("[X] no RFC connection (rfc: ashost) for this system -- custom alerts need RFC")
        return 1
    print(f"[OK] RFC connection configured (host {rfc.get('ashost')}, client {rfc.get('client') or cfg.get('client')})")

    from core.check_alerts import _eligible
    blocked = False
    if not _eligible(cfg):
        why = CA._why_not_eligible(cfg)
        print(f"[X] {why}")
        blocked = True
    else:
        print("[OK] not parked, not in maintenance, not on standby, reachable")
    from core import reachability
    vpn = reachability.precheck(name, cfg)
    if vpn:
        print(f"[X] {vpn}")
        blocked = True
    if blocked and not force:
        print("\nThe 5-minute check skips this system for the reason above. Fix that (e.g. clear the parked flag "
              "on the wall after fixing the SAP user/password), or run again with --force to test the RFC reads.")
        return 1

    print("... logging on over RFC and reading (read-only) ...")
    from collectors.checks_job import collect_custom_job
    raw = collect_custom_job(name, cfg)
    if not raw.get("ok"):
        print(f"[X] RFC logon failed: {raw.get('error')}")
        return 1
    print("[OK] RFC logon")
    data = raw["data"]
    parts = [("wp", "work processes (TH_WPINFO)", lambda d: f"{len(d.get('instances') or [])} instance(s)"),
             ("dumps", "ABAP dumps (/SDF/GET_DUMP_LOG)", lambda d: f"{len(d.get('rows') or [])} dump(s) today"),
             ("cancelled", "cancelled jobs (TBTCO)", lambda d: f"{len(d.get('rows') or [])} row(s)"),
             ("running", "running jobs (TBTCO)", lambda d: f"{len(d.get('rows') or [])} row(s)"),
             ("locks", "lock entries (ENQUE_READ2)", lambda d: f"{len(d.get('rows') or [])} lock(s)"),
             ("sm58", "failed tRFC (ARFCSSTATE)", lambda d: f"{d.get('count')} entr(ies)"),
             ("smq2", "inbound queues (TRFCQIN)", lambda d: f"{len(d.get('rows') or [])} entr(ies)")]
    bad = 0
    for key, label, ok_text in parts:
        d = data.get(key) or {}
        if d.get("error"):
            bad += 1
            print(f"[X] {label}: {d['error']}")
        else:
            print(f"[OK] {label}: {ok_text(d)}")

    rows = (data.get("dumps") or {}).get("rows") or []
    if rows:
        from collections import Counter
        top = Counter(str(r.get("user") or "?").upper() for r in rows).most_common(3)
        print("      dumps today by user: " + ", ".join(f"{u} {n}" for u, n in top))

    # Settings only: the values themselves are worked out by the 5-minute check,
    # which also remembers what it has seen -- this script does not touch that.
    print("\n== Alert settings for this system (Thresholds & alerts page)")
    from core import alert_engine, grading
    off = []
    for key, label in CA.LABELS.items():
        rule = alert_engine.rule_for(name, key)
        lim = grading.limits_for(key, name)
        print(f"  {label:<52} limits {str(lim['warning'])}/{str(lim['critical']):<6} alert: {rule['alert']:<8} "
              f"via {', '.join(rule['channels']) or 'nothing'}")
        if rule["alert"] == "off":
            off.append(label)
    if off:
        print("\n[!] alert is OFF for: " + ", ".join(off) + " -- set it on the Thresholds & alerts page")
    print("\nResult: " + ("all RFC reads work." if not bad else f"{bad} read(s) failed -- see [X] lines above."))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

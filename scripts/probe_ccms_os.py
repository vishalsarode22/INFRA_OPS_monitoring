"""
Live probe for the CCMS (RZ20) OS reader that fills CPU / memory / load
on systems without the Z FM or an SMON schedule.

    python scripts/probe_ccms_os.py CENTOR_QAS            # what read_os() returns
    python scripts/probe_ccms_os.py CENTOR_QAS --tree     # every MTE name in the OS monitor
    python scripts/probe_ccms_os.py CENTOR_QAS --monitors # every monitor set / monitor
    python scripts/probe_ccms_os.py CENTOR_QAS --raw      # BAPI exceptions, not swallowed

First run tells you one of three things:
  * XMI logon failed        -> RFC user needs S_XMI_PROD (interface XAL)
  * tree has N nodes, none matched -> use --tree, add the names to
                                      collectors/ccms_os._ROLES
  * matched but values None -> saposcol not running on that host (ST06 empty too)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import ccms_os as cc
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    cfg = next((s for s in get_systems() if s["name"] == name), None)
    if not cfg:
        print(f"No system named {name} in config/systems.yaml")
        return 2

    with SapSession(name, cfg) as s:
        if not s.ok:
            print("CONNECT FAILED:", s.error)
            return 1

        if "--raw" in sys.argv:
            # session.call() swallows exceptions; here we want to see them.
            try:
                print("BAPI_XMI_LOGON ->", s.conn.call(
                    "BAPI_XMI_LOGON", EXTCOMPANY=cc.XMI_COMPANY, EXTPRODUCT=cc.XMI_PRODUCT,
                    INTERFACE=cc.XMI_INTERFACE, VERSION=cc.XMI_VERSION))
                ms, mo = cc._os_monitor_name(s)
                print(f"monitor -> {ms!r} / {mo!r}")
                got = s.conn.call("BAPI_SYSTEM_MON_GETTREE", EXTERNAL_USER_NAME=cc.EXT_USER,
                                  MONITOR_NAME={"MS_NAME": ms, "MONI_NAME": mo})
                nodes = got.get("TREE_NODES") or []
                print(f"TREE_NODES: {len(nodes)}  RETURN: {got.get('RETURN')}")
                if nodes:
                    print("first node keys:", sorted(nodes[0].keys()))
                    print("first node:", json.dumps(nodes[0], indent=1, default=str)[:1200])
            except Exception as exc:  # noqa: BLE001
                print(f"RAW EXCEPTION: {type(exc).__name__}: {exc}")
            finally:
                s.call("BAPI_XMI_LOGOFF", INTERFACE=cc.XMI_INTERFACE)
            return 0

        if "--monitors" in sys.argv:
            err = cc._logon(s)
            if err:
                print("XMI logon failed:", err); return 1
            got = s.call("BAPI_SYSTEM_MON_GETLIST", EXTERNAL_USER_NAME=cc.EXT_USER) or {}
            for m in got.get("MONITOR_NAMES") or []:
                print(f"  {m.get('MS_NAME','')!s:40} | {m.get('MONI_NAME','')}")
            cc._logoff(s)
            return 0

        if "--tree" in sys.argv:
            err = cc._logon(s)
            if err:
                print("XMI logon failed:", err); return 1
            cc.reset_cache(name)
            tree, seen, err = cc._discover(s, name)
            cc._logoff(s)
            print(f"nodes seen: {len(seen)}   error: {err}")
            for n in seen:
                role = cc._role_for(n)
                print(f"  {'[' + role + ']' if role else '':12} {n}")
            return 0

        cc.reset_cache(name)
        out = cc.read_os(s, name)

    print(f"error:   {out['error']}")
    print(f"cpu:     {out['cpu']}")
    print(f"memory:  {out['memory']}")
    print(f"load_1m: {out['load_1m']}   load_5m: {out['load_5m']}")
    print("per_host:")
    print(json.dumps(out["per_host"], indent=2, default=str))
    if out["seen"] and not out["per_host"]:
        print(f"\n{len(out['seen'])} MTE names seen, none matched. Run with --tree to list them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

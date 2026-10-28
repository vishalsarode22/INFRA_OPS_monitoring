"""
Which extra checks work on a system, and why not where they do not.

    python scripts/probe_checks.py PS4
    python scripts/probe_checks.py PS4 --json

Runs every check in collectors/extra_checks.py once (no cache) and prints
the result, plus which optional sources exist on that system: the licence
readers, EM_GET_NUMBER_OF_ENTRIES, TH_GET_PARAMETER, CAT_CHECK_RFC_DESTINATION,
the XBP job functions used by one-click actions, and the TPALOG fields.
Read-only: it changes nothing in SAP.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(".env")

from collectors import extra_checks  # noqa: E402
from collectors.rfc_collector import SapSession  # noqa: E402
from core.config_loader import get_all_systems  # noqa: E402

FMS = ["DDIF_FIELDINFO_GET", "EM_GET_NUMBER_OF_ENTRIES", "TH_GET_PARAMETER", "CAT_CHECK_RFC_DESTINATION",
       "SLIC_GET_LICENCE_KEYS", "SLIC_LIKEY_GET_ALL_LICENSES", "BAPI_XMI_LOGON", "BAPI_XBP_JOB_COPY",
       "BAPI_XBP_JOB_START_IMMEDIATELY", "BAPI_XBP_JOB_ABORT", "BAPI_USER_UNLOCK", "ENQUE_READ2",
       "ENQUE_DELETE", "TRFC_QIN_RESTART", "RFC_SYSTEM_INFO"]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    as_json = "--json" in sys.argv
    cfg = next((c for c in get_all_systems() if c.get("name") == name), None)
    if cfg is None:
        print(f"unknown system {name}; known: {', '.join(c.get('name') for c in get_all_systems())}")
        return 2
    out = {"system": name, "functions": {}, "checks": []}
    with SapSession(name, cfg) as s:
        if not s.ok:
            print(f"RFC logon failed: {s.error}")
        else:
            for fm in FMS:
                p = extra_checks.iface(s, fm)
                out["functions"][fm] = None if p is None else sorted(
                    f"{v.get('direction', '?').replace('RFC_', '')}:{k}" for k, v in p.items())
            out["tpalog_fields"] = sorted(extra_checks.table_fields(s, "TPALOG") or [])
        for m in extra_checks.run(s, name, cfg):
            out["checks"].append({"metric": m.name, "status": m.status.value, "value": m.display_value,
                                  "detail": m.detail})
    if as_json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"== {name}: function modules")
    for fm, params in out["functions"].items():
        print(f"  {'OK ' if params is not None else '-- '} {fm}" + (f"  ({', '.join(params[:8])}"
              f"{' ...' if len(params) > 8 else ''})" if params else ""))
    if "tpalog_fields" in out:
        print(f"== TPALOG fields: {', '.join(out['tpalog_fields']) or 'not readable'}")
    print("== checks")
    for c in out["checks"]:
        print(f"  {c['status']:<8} {c['metric']:<32} {c['value']:<12} {c['detail'][:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

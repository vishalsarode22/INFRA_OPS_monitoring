r"""
Last-ditch hunt for SMLG's live response/users/quality over PURE RFC.

The obvious load FMs are all FU_NOT_FOUND on this kernel. But the same
figures are ALSO published into the CCMS monitoring architecture -- SMLG's
own REPORT_TO_MONI_ARCH writes quality to
  \\\\<SID>\<server>\R3Services\Dialog\LogonLoadQuality
and the dialog response time is usually the sibling node. CCMS MTEs are read
with BAPI_SYSTEM_MTE_GETPERFCURVAL, which IS remote-enabled (the OS collector
path uses it). If the nodes exist, we get SMLG's numbers with no Z object.

This probe:
  1. Tries a batch of less-common thread/workload FMs.
  2. Walks the CCMS tree (BAPI_SYSTEM_MTE_GET_TREE / _GETALTREE) and prints
     every node whose path mentions Dialog / R3Services / Response / Quality
     / LogonLoad, with its current value.

Usage:
    python scripts/probe_smlg_rfc.py "CENTOR_QAS"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.rfc_collector import SapSession            # noqa: E402
from core.config_loader import get_systems                 # noqa: E402

EXT_USER = "IBO_PROBE"

EXTRA_FMS = (
    "TH_GET_VIRT_SRV_LIST",
    "TH_WPINFO",
    "SALC_MODES_MEASURE",
    "SAPWL_WORKLOAD_GET_STATISTIC",
    "SAPWL_SERVERS_GET",
    "TH_LOAD_MONITOR",
    "RZL_READ_DIR_LOCAL",
    "SXMS_GET_MESSAGE_SERVER",
    "SMLG_GET_LOAD",
)

KEYWORDS = ("dialog", "r3services", "response", "quality", "logonload",
            "resptime", "answer")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("system")
    ap.add_argument("--dump-all", action="store_true",
                    help="print the ENTIRE CCMS node list, not just matches")
    args = ap.parse_args()

    cfg = next((s for s in get_systems() if s.get("name") == args.system), None)
    if cfg is None:
        print(f"Unknown system: {args.system}")
        return 2

    with SapSession(args.system, cfg) as session:
        if not session.ok:
            print(f"No RFC session: {session.error}")
            return 1
        conn = session.conn

        print("=" * 60)
        print("Extra thread/workload FMs")
        for fm in EXTRA_FMS:
            try:
                got = conn.call(fm)
            except Exception as exc:
                print(f"  {fm}: {type(exc).__name__}: {str(exc)[:90]}")
                continue
            print(f"  {fm}: OK -> tables/fields: {list((got or {}).keys())}")

        print("=" * 60)
        print("CCMS tree walk for Dialog / R3Services / Response / Quality")
        tree = None
        for tree_fm in ("BAPI_SYSTEM_MTE_GETMLBYNAME",
                        "BAPI_SYSTEM_MT_GETTREE",
                        "BAPI_SYSTEM_MTE_GETTREE"):
            try:
                tree = conn.call(tree_fm, EXTERNAL_USER_NAME=EXT_USER)
                print(f"  (tree via {tree_fm})")
                break
            except Exception as exc:
                print(f"  {tree_fm}: {type(exc).__name__}: {str(exc)[:70]}")
        if not tree:
            # Fall back to the same discovery the ccms_os collector uses.
            try:
                from collectors import ccms_os
                nodes = ccms_os._discover_tree(session, args.system) \
                    if hasattr(ccms_os, "_discover_tree") else None
                print(f"  ccms_os discovery: {type(nodes)}")
            except Exception as exc:
                print(f"  ccms_os discovery failed: {exc}")
            return 0

        rows = None
        for key in ("TREE_NODES", "NODES", "MT_NAMES", "TREE"):
            if isinstance(tree.get(key), list):
                rows = tree[key]
                break
        if not rows:
            print(f"  tree keys: {list(tree.keys())}")
            return 0

        # Show the COLUMN names once so we know what a node looks like.
        if rows and isinstance(rows[0], dict):
            print(f"  node columns: {list(rows[0].keys())}")
        printed = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            blob = " ".join(str(v) for v in r.values()).lower()
            if args.dump_all or any(k in blob for k in KEYWORDS):
                print(f"    {r}")
                printed += 1
            if printed > 250:
                print("    ... (truncated)")
                break
        if not printed:
            print(f"  {len(rows)} nodes, none matched keywords. Distinct "
                  f"MTCLASS/OBJECTNAME values follow so we can spot the right tree:")
            seen = set()
            for r in rows:
                if not isinstance(r, dict):
                    continue
                tag = (str(r.get("OBJECTNAME", "")), str(r.get("MTCLASS", "")),
                       str(r.get("SHORTNAME", r.get("MTNAMESHRT", ""))))
                if tag not in seen:
                    seen.add(tag)
                    print(f"    OBJ={tag[0]!r} CLASS={tag[1]!r} NAME={tag[2]!r}")
                if len(seen) > 200:
                    break

        # The workload path: SAPWL is remote-enabled here and its
        # TIME_STATISTIC / INSTANCES tables carry average dialog response per
        # server -- ST03 numbers, close to what SMLG shows.
        print("=" * 60)
        print("SAPWL_WORKLOAD_GET_STATISTIC -- response fields")
        try:
            wl = conn.call("SAPWL_WORKLOAD_GET_STATISTIC")
            for tbl in ("INSTANCES", "TIME_STATISTIC", "TASKTYPE_STATISTIC"):
                data = wl.get(tbl) or []
                print(f"  {tbl}: {len(data)} rows")
                if data and isinstance(data[0], dict):
                    print(f"    columns: {list(data[0].keys())}")
                    for row in data[:3]:
                        print(f"    {row}")
        except Exception as exc:
            print(f"  call needs params: {type(exc).__name__}: {str(exc)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

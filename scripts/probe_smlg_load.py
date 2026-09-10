"""
Dump the raw message-server load table behind SMLG's instance view.

The dashboard instance response now reads TH_LOAD_DISTRIBUTION so it matches
transaction SMLG. That FM's column names vary by kernel release, and the
reader in rfc_live guesses among the common ones. If the instance response
still does not match SMLG after the patch, run this: it prints the exact
table and column names your kernel uses, which is what the reader needs.

Usage:
    python scripts/probe_smlg_load.py "SARLOHA PRD"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.rfc_collector import SapSession            # noqa: E402
from core.config_loader import get_systems                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("system")
    args = ap.parse_args()

    cfg = next((s for s in get_systems() if s.get("name") == args.system), None)
    if cfg is None:
        print(f"Unknown system: {args.system}")
        return 2

    with SapSession(args.system, cfg) as session:
        if not session.ok:
            print(f"No RFC session: {session.error}")
            return 1

        candidates = (
            "TH_LOAD_DISTRIBUTION",
            "TH_GET_LOAD_DISTRIBUTION",
            "SMLG_GET_LOAD_DISTRIBUTION",
            "TH_SERVER_LIST",
            "TH_SYSTEMINFO",
            "SMLG_GET_DEFINED_GROUPS",
            "SMLG_GET_SETUP",
        )

        # THE ONE THAT MATTERS: SMLG's actual source (traced through
        # SAPMSMLG include MSMLGF02). Try it against every server name plus
        # blank, and show which VALUE1 record types come back -- if this
        # release uses a type code other than 7353 for the load records,
        # the histogram exposes it immediately.
        from collections import Counter
        try:
            servers = [str(r.get("NAME", "")).strip()
                       for r in (session.conn.call("TH_SERVER_LIST") or {}).get("LIST", [])]
        except Exception:
            servers = []
        print("=" * 60)
        print("RZL_INTG_READALL_C (SMLG's source; trying each SRVNAME)")
        for srv in servers + [""]:
            try:
                got = session.conn.call("RZL_INTG_READALL_C", SRVNAME=srv)
            except Exception as exc:
                print(f"  SRVNAME={srv!r}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            rows = got.get("INTG_TBL") or []
            types = Counter(str(r.get("VALUE1", "")).strip() for r in rows
                            if isinstance(r, dict))
            print(f"  SRVNAME={srv!r}: {len(rows)} rows; VALUE1 types: "
                  f"{dict(types.most_common(10))}")
            shown = 0
            for r in rows:
                if str(r.get("VALUE1", "")).strip() in ("7353",) or shown < 3:
                    print(f"    {r}")
                    shown += 1
                if shown >= 8:
                    break

        for fm in candidates:
            print("=" * 60)
            print(f"Calling {fm}")
            try:
                got = session.conn.call(fm)
            except Exception as exc:
                print(f"  not callable: {type(exc).__name__}: {exc}")
                continue

            # Show every exported parameter and the shape of each table.
            for key, value in got.items():
                if isinstance(value, list):
                    print(f"  TABLE {key}: {len(value)} rows")
                    if value and isinstance(value[0], dict):
                        cols = list(value[0].keys())
                        print(f"    columns: {cols}")
                        for row in value[:4]:
                            compact = {c: row.get(c) for c in cols}
                            print(f"    {compact}")
                else:
                    print(f"  FIELD {key} = {value!r}")

        # The logon-group table itself, for comparison. SMLG's GUI values
        # come from message-server memory, not this table, which is why
        # RZLLITAB.RESP_TIME reads 0 while SMLG shows 156 ms.
        print("=" * 60)
        print("RFC_READ_TABLE RZLLITAB (config table, for comparison)")
        try:
            res = session.conn.call("RFC_READ_TABLE", QUERY_TABLE="RZLLITAB",
                                    DELIMITER="|", ROWCOUNT=50)
            cols = [f["FIELDNAME"] for f in res.get("FIELDS", [])]
            print(f"  columns: {cols}")
            for row in res.get("DATA", [])[:10]:
                print(f"  {row.get('WA')}")
        except Exception as exc:
            print(f"  not readable: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

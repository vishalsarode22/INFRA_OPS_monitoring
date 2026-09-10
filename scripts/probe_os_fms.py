"""
Find which OS-monitor function modules this kernel exposes over RFC.

Same method that cracked the SMLG figures: call every plausible candidate,
print exactly what each returns (or FU_NOT_FOUND). CPU and memory
ultimately come from saposcol; ST06/OS07N read it through FMs that vary by
release, so rather than guess in the collector, this shows what exists.

If saposcol is NOT running on the host, everything below returns zeros or
empty -- fix that first (ST06 -> start collector).

Usage:
    python scripts/probe_os_fms.py "SARLOHA QAS"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.rfc_collector import SapSession            # noqa: E402
from core.config_loader import get_systems                 # noqa: E402

# Candidates, roughly old-ST06 family first. FU_NOT_FOUND is harmless and
# is itself the answer for that name.
CANDIDATES = (
    ("GET_CPU_ALL", {}),
    ("GET_MEM_ALL", {}),
    ("GET_SWAP_ALL", {}),
    ("GET_OS_TYPE", {}),
    ("GET_HIST_DATA_ALL", {}),
    ("OS_INFO", {}),
    ("SAPOSCOL_GET_DATA", {}),
    ("TH_OSMON", {}),
    ("RZL_READ_OS", {}),
    ("SALO_GET_OSMON_DATA", {}),
    ("SAPTUNE_GET_SUMMARY_STATISTIC", {}),   # ST02 -- known to work, control
)


def dump(got, limit_rows=4):
    for key, value in (got or {}).items():
        if isinstance(value, list):
            print(f"    TABLE {key}: {len(value)} rows")
            if value and isinstance(value[0], dict):
                print(f"      columns: {list(value[0].keys())}")
                for row in value[:limit_rows]:
                    print(f"      {row}")
        elif isinstance(value, dict):
            print(f"    STRUCT {key}: {value}")
        else:
            print(f"    FIELD {key} = {value!r}")


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
        for fm, params in CANDIDATES:
            print("=" * 60)
            print(f"CALL {fm} {params or ''}")
            try:
                got = session.conn.call(fm, **params)
            except Exception as exc:
                msg = str(exc)
                print(f"  -> {type(exc).__name__}: {msg[:160]}")
                continue
            dump(got)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Show the raw dialog-response distribution behind sap.st03.dialog_resp_ms.

The wall reported 464549 ms on SARLOHA PRD while the same card showed 0
users and 2 of 72 work processes busy. Those two things cannot both be a
plain reading of a healthy system, and the last time a figure looked like
this the "obvious" /1000 unit fix was wrong -- the number was real, just
computed over too few samples.

So: do not adjust the statistic until you have looked at the values it is
computed from. This prints every dialog step in the window, the percentile
ladder, and the individual steps at the top of the tail, which is enough to
tell the three candidate causes apart:

  * a unit problem      -> every value is uniformly ~1000x too large
  * a task-type problem -> the steps are not interactive dialog at all
                           (check the TASKTYPE mix line)
  * a long tail         -> most steps are tens of ms and a handful are
                           hundreds of seconds; the median is fine and the
                           mean is the thing that was lying

Usage:
    python scripts/probe_dialog_response.py SARLOHA_PRD
    python scripts/probe_dialog_response.py SARLOHA_PRD --window 15
    python scripts/probe_dialog_response.py SARLOHA_PRD --raw 40
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import rfc_perf as rp                      # noqa: E402
from collectors.rfc_collector import SapSession            # noqa: E402
from core.config_loader import get_systems                 # noqa: E402


def pct(values, q):
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("system")
    ap.add_argument("--window", type=int, default=None,
                    help="minutes of STAT records to read (default: IBO_STAT_WINDOW_MINUTES)")
    ap.add_argument("--raw", type=int, default=20,
                    help="how many of the slowest individual steps to print")
    args = ap.parse_args()

    if args.window:
        rp.RESP_WINDOW_MINUTES = args.window

    cfg = next((s for s in get_systems() if s.get("name") == args.system), None)
    if cfg is None:
        print(f"Unknown system: {args.system}")
        print("Configured:", ", ".join(s.get("name", "?") for s in get_systems()))
        return 2

    with SapSession(args.system, cfg) as session:
        if not session.ok:
            print(f"No RFC session: {session.error}")
            return 1

        inst = rp.instances(session)
        print(f"Instances: {[i['name'] for i in inst]}")

        recs, note = rp.stat_records(session, inst, args.system)
        print(f"STAT records in last {rp.RESP_WINDOW_MINUTES} min: {len(recs)}"
              + (f"  ({note})" if note else ""))
        if not recs:
            print("Nothing to analyse. Either the window is idle or stat/level "
                  "is 0 in RZ11.")
            return 0

        mix = Counter(rp._task_label(rp._tasktype_code(r.get("TASKTYPE"))) for r in recs)
        print("Task mix:", ", ".join(f"{k}={v}" for k, v in mix.most_common()))

        dia = [r for r in recs if rp._tasktype_is_dialog(r.get("TASKTYPE"))]
        print(f"Dialog steps: {len(dia)}")
        if not dia:
            print("No dialog steps decoded -- if the mix above shows DIALOG, "
                  "the TASKTYPE decode is the bug, not the arithmetic.")
            return 0

        resp = [rp._num(r.get("RESPTI")) or 0 for r in dia]
        print()
        print("RESPTI ladder (ms):")
        for label, q in (("min", 0.0), ("p25", 0.25), ("median", 0.5),
                         ("p75", 0.75), ("p90", 0.90), ("p95", 0.95),
                         ("p99", 0.99), ("max", 1.0)):
            print(f"  {label:>7} {round(pct(resp, q)):>12,}")
        print(f"  {'mean':>7} {round(sum(resp) / len(resp)):>12,}")
        print()
        print("If median is small and mean is large, the tile was reporting "
              "the tail, not the experience. That is what the median headline "
              "in response_summary() now fixes.")
        print()

        rows = sorted(
            ({"resp": rp._num(r.get("RESPTI")) or 0,
              "user": rp._s(r.get("ACCOUNT"), 24),
              "report": rp._s(r.get("REPORT"), 40),
              "tcode": rp._s(r.get("TCODE"), 20),
              "inst": r.get("_instance", "")} for r in dia),
            key=lambda d: d["resp"], reverse=True)[:args.raw]
        print(f"Slowest {len(rows)} individual steps:")
        print(f"  {'ms':>12}  {'user':<14} {'tcode':<10} {'report':<32} instance")
        for d in rows:
            print(f"  {round(d['resp']):>12,}  {d['user']:<14} {d['tcode']:<10} "
                  f"{d['report']:<32} {d['inst']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

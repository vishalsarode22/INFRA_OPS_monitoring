"""
Live probe for the RFC performance collector.

    python scripts/probe_rfc_perf.py TST
    python scripts/probe_rfc_perf.py TST --shape SQLMD
    python scripts/probe_rfc_perf.py TST --shape /SDF/SMON_WPINFO
    python scripts/probe_rfc_perf.py TST --stat       # raw STAT record diagnostics
    python scripts/probe_rfc_perf.py TST --sqlm       # is SQLMD populated? sample rows
    python scripts/probe_rfc_perf.py TST --locks      # raw ENQ record keys and sample

Prints every metric with its status and detail, then the raw per-instance
data, so the first live run tells you which sources answered and which
need their candidate columns adjusted. --shape dumps the columns a table
actually has on this release via DDIF_FIELDINFO_GET.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import rfc_perf as rp
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

    if "--why" in sys.argv:
        # Instruments the ACTUAL response path for one system: which source
        # fired, the dialog subset, its raw RESPTI values, and the computed
        # avg/max. This is the ground truth when a tile shows a wrong number.
        with SapSession(name, cfg) as s:
            if not s.ok:
                print("CONNECT FAILED:", s.error); return 1
            inst = rp.instances(s)
            print("instances:", inst)
            recs, note = rp.stat_records(s, inst, name)
            print(f"stat_records -> {None if recs is None else len(recs)} records; note={note!r}")
            if recs is not None:
                dia = [r for r in recs if rp._tasktype_is_dialog(r.get("TASKTYPE"))]
                print(f"dialog records (via _tasktype_is_dialog): {len(dia)} of {len(recs)}")
                vals = [rp._num(r.get("RESPTI")) or 0 for r in dia]
                print(f"dialog RESPTI values: {sorted(vals, reverse=True)[:20]}"
                      + (" ..." if len(vals) > 20 else ""))
                if vals:
                    print(f"  sum={sum(vals):.0f}  count={len(vals)}  "
                          f"avg={sum(vals)/len(vals):.1f}  max_single={max(vals):.0f}")
                # show what task types survived the dialog filter
                from collections import Counter
                tt = Counter(str(r.get("TASKTYPE")) for r in dia)
                print("  dialog task types kept:", dict(tt))
                rs = rp.response_summary(recs, name)
            else:
                rs = rp.st03n_aggregate(s, inst)
                print("using ST03N aggregate")
            print("\nresponse_summary result:")
            if rs:
                for k in ("steps", "avg_resp_ms", "max_instance_resp_ms",
                          "per_instance", "db_time_pct"):
                    print(f"  {k} = {rs.get(k)}")
                print("  top_users_by_total_ms =", rs.get("top_users_by_total_ms"))
        return 0

    if "--shape" in sys.argv:
        table = sys.argv[sys.argv.index("--shape") + 1]
        with SapSession(name, cfg) as s:
            if not s.ok:
                print("CONNECT FAILED:", s.error); return 1
            fields = rp.table_fields(s, name, table)
        print(f"{table}: {len(fields)} columns")
        for f in fields:
            print("  ", f)
        return 0

    if "--sqlm" in sys.argv:
        with SapSession(name, cfg) as s:
            if not s.ok:
                print("CONNECT FAILED:", s.error); return 1
            have = rp.table_fields(s, name, "SQLMD")
            print("SQLMD columns:", len(have))
            cols = {r: rp._pick(have, c) for r, c in rp._SQLM_ROLES.items()}
            print("role mapping:", json.dumps(cols, indent=2))
            probe = [c for c in (cols["program"], cols["execs"], cols["total_us"], cols["date"]) if c]
            rows = s.read_table("SQLMD", probe, "", rows=5)
            print("unfiltered read (5 rows):", "FAILED" if rows is None else f"{len(rows)} rows")
            for r in rows or []:
                print("  ", r)
            if cols["date"]:
                from datetime import datetime, timedelta
                since = (datetime.now() - timedelta(days=rp.SQLM_LOOKBACK_DAYS)).strftime("%Y%m%d")
                rows = s.read_table("SQLMD", probe, f"{cols['date']} >= '{since}'", rows=5)
                print(f"filtered read ({cols['date']} >= {since}):",
                      "FAILED" if rows is None else f"{len(rows)} rows")
                for r in rows or []:
                    print("  ", r)
        return 0

    if "--locks" in sys.argv:
        client = str((cfg.get("rfc") or {}).get("client") or cfg.get("client") or "000")
        with SapSession(name, cfg) as s:
            if not s.ok:
                print("CONNECT FAILED:", s.error); return 1
            inst = rp.instances(s)
            rp.stat_records(s, inst, name)          # learns the SAP clock
            from datetime import datetime as _dt
            print("host clock:", _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                  " sap clock:", rp.sap_now(name).strftime("%Y-%m-%d %H:%M:%S"),
                  f"({'learned' if name in rp._sap_clock else 'NOT learned, using host'})")
            summary = rp.lock_summary(s, client, name)     # infers the owner-stamp offset
            off = rp._owner_offset_min.get(name, 0)
            print(f"owner-stamp offset vs SAP time: {off} min" + ("  <-- OS TZ mismatch on app server" if off else ""))
            res = s.call("ENQUE_READ2", GCLIENT=client, GUNAME="")
            enq = (res or {}).get("ENQ", [])
            print(f"ENQ entries: {len(enq)}  (client {client})  oldest: {summary['oldest_minutes']} min")
            from datetime import timedelta as _td
            ref = rp.sap_now(name) + _td(minutes=off)
            for e in enq[:12]:
                st = rp._lock_timestamp(e)
                age = round((ref - st).total_seconds() / 60, 1) if st else None
                print(f"  {rp._s(e.get('GUNAME'),12):12} {rp._s(e.get('GNAME'),14):14} "
                      f"set={st}  age_min={age}  GUSR={rp._s(e.get('GUSR'),30)}")
        return 0

    if "--stat" in sys.argv:
        with SapSession(name, cfg) as s:
            if not s.ok:
                print("CONNECT FAILED:", s.error); return 1
            inst = rp.instances(s)
            print("instances:", inst)
            recs, note = rp.stat_records(s, inst)
            print(f"records in last {rp.RESP_WINDOW_MINUTES} min: {len(recs)}  note: {note or '-'}")
            print("STAT result shape (key, len/type):", rp._last_stat_result_keys.get("shape"))
            if recs:
                print("keys:", sorted(recs[0].keys()))
                from collections import Counter
                print("TASKTYPE values:", Counter(repr(r.get("TASKTYPE")) for r in recs).most_common(10))
                print("sample:", json.dumps(recs[0], indent=2, default=str)[:1500])
            else:
                # Widen the window to see whether the FM returns anything at all
                rp.RESP_WINDOW_MINUTES = 120
                recs, note = rp.stat_records(s, inst)
                print(f"records in last 120 min: {len(recs)}")
                if recs:
                    print("keys:", sorted(recs[0].keys()))
                    from collections import Counter
                    print("TASKTYPE values:", Counter(repr(r.get("TASKTYPE")) for r in recs).most_common(10))
            if recs:
                print("MAINREC keys (all):", sorted(k for k in recs[0].keys() if not isinstance(recs[0][k], list)))
                print("sub-records:", sorted(k for k in recs[0].keys() if isinstance(recs[0][k], list)))
            print("\n--- ST03N aggregate fallback (raw call, exception shown) ---")
            from datetime import datetime as _dt
            sid = rp._sid_from_instance(inst[0]["name"]) if inst else ""
            for comp in ([inst[0]["name"]] if inst else []) + ["TOTAL"]:
                try:
                    raw = s.conn.call("SWNC_COLLECTOR_GET_AGGREGATES", COMPONENT=comp, ASSIGNDSYS=sid,
                                      PERIODTYPE="D", PERIODSTRT=_dt.now().strftime("%Y%m%d"))
                    print(f"COMPONENT={comp}: OK ->", [(k, len(v) if isinstance(v, list) else type(v).__name__)
                                                       for k, v in raw.items()])
                    tt = raw.get("TASKTIMES") or []
                    if tt:
                        print("  TASKTIMES[0] keys:", sorted(tt[0].keys()))
                        print("  TASKTIMES sample:", {k: str(v)[:20] for k, v in list(tt[0].items())[:12]})
                    break
                except Exception as e:
                    print(f"COMPONENT={comp}: {type(e).__name__}: {str(e)[:300]}")
        return 0

    import time as _t
    with SapSession(name, cfg) as s:
        if not s.ok:
            print("CONNECT FAILED:", s.error); return 1
        inst = rp.instances(s)
        timings = {}
        t0 = _t.monotonic(); rp.work_processes(s, inst); timings["TH_WPINFO x instances"] = _t.monotonic() - t0
        t0 = _t.monotonic(); recs, _ = rp.stat_records(s, inst, name); timings[f"STAT {rp.RESP_WINDOW_MINUTES}min ({len(recs)} recs)"] = _t.monotonic() - t0
        t0 = _t.monotonic(); rp.lock_summary(s, "100", name); timings["ENQUE_READ2"] = _t.monotonic() - t0
        t0 = _t.monotonic(); rp.sqlm_summary(s, name); timings["SQLMD"] = _t.monotonic() - t0
    print("--- timings (seconds) ---")
    for k, v in timings.items():
        print(f"  {k:40} {v:6.2f}")
    print()
    metrics, err = rp.collect_perf_metrics(name, cfg)
    print("ERROR:", err)
    for m in metrics:
        print(f"{m.name:42} {m.value:>10} {m.status.value:9} | {m.detail[:100]}")
    print("\n--- extra_data (first metric of each source) ---")
    seen = set()
    for m in metrics:
        src = m.name.split(".")[1]
        if src in seen:
            continue
        seen.add(src)
        print(f"\n[{m.name}]")
        print(json.dumps(m.extra_data, indent=2, default=str)[:2500])
    return 0


if __name__ == "__main__":
    sys.exit(main())

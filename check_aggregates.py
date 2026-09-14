from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    r = sess.conn.call("SWNC_COLLECTOR_GET_AGGREGATES")
    for key in ("TIMES", "TASKTYPE", "USERWORKLOAD"):
        rows = r.get(key) or []
        print(f"=== {key} ({len(rows)} rows) ===")
        for row in rows[:5]:
            print(row)
        print()

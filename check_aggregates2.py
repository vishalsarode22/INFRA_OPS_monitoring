from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
import datetime

s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
now = datetime.datetime.now()
today = now.strftime("%Y%m%d")

with SapSession(s["name"], s) as sess:
    try:
        r = sess.conn.call(
            "SWNC_COLLECTOR_GET_AGGREGATES",
            READ_START_DATE=today,
            READ_START_TIME="000000",
            READ_END_DATE=today,
            READ_END_TIME=now.strftime("%H%M%S"),
        )
        for key in ("TASKTYPE", "TIMES", "USERWORKLOAD"):
            rows = r.get(key) or []
            print(f"=== {key} ({len(rows)} rows) ===")
            for row in rows[:8]:
                print(row)
            print()
    except Exception as e:
        print("EXCEPTION:", type(e).__name__, str(e)[:300])

from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
import datetime

s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
today = datetime.datetime.now().strftime("%Y%m%d")

with SapSession(s["name"], s) as sess:
    for ptype in ["H", "D"]:
        try:
            r = sess.conn.call(
                "SWNC_COLLECTOR_GET_AGGREGATES",
                PERIODTYPE=ptype,
                PERIODSTRT=today,
            )
            tt = r.get("TASKTYPE") or []
            print(f"PERIODTYPE={ptype} -> TASKTYPE rows: {len(tt)}")
            for row in tt[:10]:
                print("  ", row)
        except Exception as e:
            print(f"PERIODTYPE={ptype} -> EXCEPTION:", type(e).__name__, str(e)[:200])

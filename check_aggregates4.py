from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
import datetime

s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
today = datetime.datetime.now().strftime("%Y%m%d")

with SapSession(s["name"], s) as sess:
    for comp in ["vhrrncaqci_CAQ_00", "CAQ", ""]:
        for ptype in ["H", "D"]:
            try:
                r = sess.conn.call(
                    "SWNC_COLLECTOR_GET_AGGREGATES",
                    COMPONENT=comp,
                    PERIODTYPE=ptype,
                    PERIODSTRT=today,
                )
                tt = r.get("TASKTYPE") or []
                print(f"COMPONENT={comp!r} PERIODTYPE={ptype} -> TASKTYPE rows: {len(tt)}")
                if tt:
                    for row in tt[:6]:
                        print("    ", row)
            except Exception as e:
                print(f"COMPONENT={comp!r} PERIODTYPE={ptype} -> EXCEPTION:", type(e).__name__, str(e)[:150])

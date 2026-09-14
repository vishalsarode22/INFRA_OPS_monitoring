from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    r = sess.conn.call(
        "SWNC_COLLECTOR_GET_AGGREGATES",
        COMPONENT="vhrrncaqci_CAQ_00",
        PERIODTYPE="D",
        PERIODSTRT="20260911",
        FACTOR=1,
    )
    for row in r.get("TASKTYPE") or []:
        tt = row["TASKTYPE"]
        count = float(row["COUNT"])
        respti = float(row["RESPTI"])
        avg_raw = respti / count if count else 0
        print(f"TASKTYPE={tt!r}  COUNT={count:.0f}  RESPTI={respti:.0f}  "
              f"avg(raw)={avg_raw:.1f}  avg(/1000)={avg_raw/1000:.1f}ms  avg(/1)={avg_raw:.1f}us->{avg_raw/1000:.3f}ms")

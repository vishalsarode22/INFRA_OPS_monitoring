from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    r = sess.conn.call(
        "SWNC_COLLECTOR_GET_AGGREGATES",
        COMPONENT="vhrrncaqci_CAQ_00",
        PERIODTYPE="D",
        PERIODSTRT="20260911",
        SUMMARY_ONLY="X",
    )
    times = r.get("TIMES") or []
    for row in times[:5]:
        print(row)

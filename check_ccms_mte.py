from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        for fm in ["SALR_MTE_GET_TREE", "SALR_MTE_GET_TID_BY_NAME",
                   "SALR_MTE_PERF_READ_CUR_VAL", "SWNC_COLLECTOR_GET_AGGREGATES"]:
            try:
                r = sess.conn.call(fm)
                print(fm, "-> OK (called with no params), keys:", list(r.keys()) if r else None)
            except Exception as e:
                msg = str(e)
                if "FU_NOT_FOUND" in msg:
                    print(fm, "-> NOT FOUND (not remote-enabled on this kernel)")
                else:
                    print(fm, "-> EXISTS but needs parameters:", type(e).__name__, msg[:150])

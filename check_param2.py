from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        try:
            r2 = sess.conn.call("TH_GET_PARAMETER", NAME="rdisp/autoabaptime")
            print("TH_GET_PARAMETER:", r2)
        except Exception as e:
            print("TH_GET_PARAMETER failed:", type(e).__name__, str(e)[:200])

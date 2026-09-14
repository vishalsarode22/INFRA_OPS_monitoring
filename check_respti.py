import traceback
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
try:
    s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
    print("system found:", s["name"])
    with SapSession(s["name"], s) as sess:
        print("sess.ok =", sess.ok)
        if not sess.ok:
            print("NOT CONNECTED:", sess.error)
        else:
            r = sess.conn.call("SWNC_GET_STATRECS_FRAME")
            print("top-level keys:", list(r.keys()) if r else None)
            frames = r.get("ALL_STATRECS") or []
            print("frame count:", len(frames))
            if frames:
                recs = frames[0].get("STATRECS") or []
                print("records in frame 0:", len(recs))
                if recs:
                    print("sample record keys:", list(recs[0].keys()))
                    print("sample record:", recs[0])
except Exception as e:
    print("EXCEPTION:", type(e).__name__, str(e))
    traceback.print_exc()

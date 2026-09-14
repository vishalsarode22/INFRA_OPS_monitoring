from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        try:
            desc = sess.conn.get_function_description("TH_GET_PARAMETER")
            for p in desc.parameters:
                print(p["name"], "|", p["parameter_type"], "|", p.get("default_value"))
        except Exception as e:
            print("EXCEPTION:", type(e).__name__, str(e)[:200])

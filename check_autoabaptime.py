from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        try:
            r = sess.conn.call("RFC_READ_TABLE", QUERY_TABLE="PAHI",
                               DELIMITER="|",
                               OPTIONS=[{"TEXT": "PARAMNAME = '"'"'rdisp/autoabaptime'"'"'"}],
                               FIELDS=[{"FIELDNAME": "PARAMVALUE"}])
            print(r.get("DATA"))
        except Exception as e:
            print("EXCEPTION:", type(e).__name__, str(e)[:300])

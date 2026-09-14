from collectors.ccms_os import read_os
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        m = read_os(sess, s["name"])
        for k, v in m.items():
            print(k, ":", v)

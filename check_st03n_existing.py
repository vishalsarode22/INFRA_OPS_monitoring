from collectors.rfc_perf import st03n_aggregate
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    inst = [{"name": "vhrrncaqci_CAQ_00"}]
    rs = st03n_aggregate(sess, inst)
    print(rs)

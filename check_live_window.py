from collectors.rfc_perf import stat_records
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    inst = [{"name": "vhrrncaqci_CAQ_00"}]
    recs, note = stat_records(sess, inst, s["name"])
    dialog = [r for r in recs if r.get("TASKTYPE") == b"\x01"]
    print("dialog steps in live window:", len(dialog))
    if dialog:
        vals = [(_r.get("RESPTI") or 0) for _r in dialog]
        print("raw RESPTI sample:", vals[:5])

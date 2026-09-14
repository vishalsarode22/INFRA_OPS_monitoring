from collectors.rfc_perf import build_metrics
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    if not sess.ok:
        print("NOT CONNECTED:", sess.error)
    else:
        metrics = build_metrics(s["name"], sess, s["client"])
        found = False
        for m in metrics:
            if m.name == "sap.st03.dialog_resp_ms":
                found = True
                print("value:", m.value, "ms")
                print("steps:", m.extra_data.get("steps"))
                print("window:", m.extra_data.get("window"))
        if not found:
            print("No metric produced - genuinely idle, live steps=0 AND no ST03N aggregate")

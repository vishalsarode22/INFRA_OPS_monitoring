import time
from collectors.rfc_perf import build_metrics
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
for i in range(10):
    with SapSession(s["name"], s) as sess:
        metrics = build_metrics(s["name"], sess, s["client"])
        found = False
        for m in metrics:
            if m.name == "sap.st03.dialog_resp_ms":
                found = True
                steps = m.extra_data.get("steps")
                slowest = m.extra_data.get("slowest_steps")
                print("[" + str(i) + "] value=" + str(m.value) + " steps=" + str(steps))
                print("    slowest=" + str(slowest))
        if not found:
            print("[" + str(i) + "] idle - no metric produced")
    time.sleep(60)

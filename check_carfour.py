from collectors.rfc_perf import build_metrics
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    metrics = build_metrics(s["name"], sess, s["client"])
    found = False
    for m in metrics:
        if m.name == "sap.st03.dialog_resp_ms":
            found = True
            print("value:", m.value, "ms")
            print("steps:", m.extra_data.get("steps"))
            print("mean_resp_ms:", m.extra_data.get("mean_resp_ms"))
            print("p95_resp_ms:", m.extra_data.get("p95_resp_ms"))
            print("max_resp_ms:", m.extra_data.get("max_resp_ms"))
            print("slowest_steps:", m.extra_data.get("slowest_steps"))
            print("top_users_by_total_ms:", m.extra_data.get("top_users_by_total_ms"))
    if not found:
        print("idle - no metric produced right now")

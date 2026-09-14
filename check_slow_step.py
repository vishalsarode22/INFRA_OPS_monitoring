from collectors.rfc_perf import build_metrics
from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    metrics = build_metrics(s["name"], sess, s["client"])
    for m in metrics:
        if m.name == "sap.st03.dialog_resp_ms":
            print("value:", m.value, "detail:", m.detail)
            print("steps:", m.extra_data.get("steps"))
            print("low_sample:", m.extra_data.get("low_sample"))
            print("slowest_steps:", m.extra_data.get("slowest_steps"))
            print("top_users_by_total_ms:", m.extra_data.get("top_users_by_total_ms"))

from collectors.rfc_collector import SapSession
from core.config_loader import get_systems
s = [x for x in get_systems() if "CARFOUR" in x["name"]][0]
with SapSession(s["name"], s) as sess:
    desc = sess.conn.get_function_description("SWNC_COLLECTOR_GET_AGGREGATES")
    for p in desc.parameters:
        print(p["name"], "|", p["parameter_type"], "|", p.get("default_value"))

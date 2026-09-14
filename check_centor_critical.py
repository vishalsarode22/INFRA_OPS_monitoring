from collectors.rfc_live import read_live
from core.config_loader import get_systems
s = [x for x in get_systems() if "CENTOR" in x["name"]][0]
p = read_live(s["name"], s, use_cache=False)
print("memory:", p.get("memory"))
print("checks:")
for c in (p.get("checks") or []):
    if c.get("status") in ("CRITICAL", "WARNING"):
        print(" ", c)

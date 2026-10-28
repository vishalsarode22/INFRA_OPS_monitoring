from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"

if not APP.exists():
    raise SystemExit(f"Dashboard app not found: {APP}")

s = APP.read_text(encoding="utf-8")

if "from core.production_health import liveness, readiness" not in s:
    anchor = "from core.config_loader import get_systems, get_systems_safe, add_system, get_monitoring_tasks"
    if anchor not in s:
        raise RuntimeError("Dashboard config import anchor not found; no changes made.")
    s = s.replace(anchor, anchor + "\nfrom core.production_health import liveness, readiness", 1)

if '@app.get("/healthz")' not in s:
    anchor = '@app.get("/")\ndef index():'
    block = '''@app.get("/healthz")\ndef healthz():\n    """Process liveness endpoint. Does not require SAP/AI connectivity."""\n    return liveness()\n\n\n@app.get("/readyz")\ndef readyz():\n    """Local readiness endpoint with safe, non-secret diagnostics."""\n    result = readiness()\n    if result["status"] != "ready":\n        # Keep the payload useful while letting load balancers distinguish a\n        # live-but-not-ready service.\n        from fastapi.responses import JSONResponse\n        return JSONResponse(status_code=503, content=result)\n    return result\n\n\n@app.get("/")\ndef index():'''
    if anchor not in s:
        raise RuntimeError("Dashboard index route anchor not found; no changes made.")
    s = s.replace(anchor, block, 1)

APP.write_text(s, encoding="utf-8")
print("Milestone 8.0 applied safely.")

from pathlib import Path
import re
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"

if not APP.exists():
    raise SystemExit(f"Cannot find {APP}")

s = APP.read_text(encoding="utf-8")

backup = APP.with_suffix(".py.pre_8_3_repair.bak")
if not backup.exists():
    shutil.copy2(APP, backup)

# Remove the known misplaced Milestone 8.3 middleware block.
start = s.find("# Milestone 8.3: safe HTTP security headers.")
if start != -1:
    end = s.find("\n\n", start)
    if end == -1:
        raise RuntimeError("Could not safely locate the end of the old middleware block.")
    # Find the next top-level declaration after the middleware function.
    next_def = re.search(r"\n(?=^[A-Za-z_][A-Za-z0-9_]*\s*=|^@app\.|^def |^async def )", s[end+2:], re.MULTILINE)
    if next_def:
        cut_end = end + 2 + next_def.start()
    else:
        cut_end = len(s)
    s = s[:start] + s[cut_end:]

# Locate the real FastAPI application declaration.
m = re.search(r'(?ms)^app\s*=\s*FastAPI\(\n.*?^\)\n', s)
if not m:
    raise RuntimeError("Could not locate app = FastAPI(...) in dashboard/app.py")

middleware = (
    '\n# Milestone 8.3: safe HTTP security headers.\n'
    '@app.middleware("http")\n'
    'async def _security_headers_middleware(request, call_next):\n'
    '    response = await call_next(request)\n'
    '    response.headers.setdefault("X-Content-Type-Options", "nosniff")\n'
    '    response.headers.setdefault("X-Frame-Options", "DENY")\n'
    '    response.headers.setdefault("Referrer-Policy", "no-referrer")\n'
    '    response.headers.setdefault(\n'
    '        "Permissions-Policy",\n'
    '        "camera=(), microphone=(), geolocation=()",\n'
    '    )\n'
    '    return response\n\n'
)

s = s[:m.end()] + middleware + s[m.end():]

# Bound history requests if not already implemented.
if "days must be between 1 and 365" not in s:
    old = (
        '@app.get("/api/history/{system_name}/{metric_name}")\n'
        'def get_history(system_name: str, metric_name: str, days: int = 5):\n'
        '    return read_metric_history(metric_name, days=days)\n'
    )
    new = (
        '@app.get("/api/history/{system_name}/{metric_name}")\n'
        'def get_history(system_name: str, metric_name: str, days: int = 5):\n'
        '    if days < 1 or days > 365:\n'
        '        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")\n'
        '    return read_metric_history(metric_name, days=days)\n'
    )
    if old in s:
        s = s.replace(old, new, 1)

# Prevent raw internal exception leakage.
s = s.replace(
    '    except Exception as e:\n        raise HTTPException(status_code=500, detail=str(e))\n',
    '    except Exception as e:\n'
    '        log.error("Failed to create system: %s", type(e).__name__)\n'
    '        raise HTTPException(status_code=500, detail="Unable to create system.")\n',
    1,
)

APP.write_text(s, encoding="utf-8")
print("Milestone 8.3 repaired and applied safely.")

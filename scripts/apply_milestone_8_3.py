from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"
if not APP.exists():
    raise SystemExit(f"dashboard/app.py not found: {APP}")

s = APP.read_text(encoding="utf-8")

pattern = re.compile(
    r'\n?# Milestone 8\.3: safe HTTP security headers\.\n'
    r'@app\.middleware\("http"\)\n'
    r'async def _security_headers_middleware\(request, call_next\):\n'
    r'(?:(?:    ).*\n)+?\n',
    re.MULTILINE,
)
s = pattern.sub("\n", s)

m = re.search(r'(?ms)^app\s*=\s*FastAPI\(\n.*?^\)\n', s)
if not m:
    raise RuntimeError("FastAPI application declaration not found.")

middleware = r'''# Milestone 8.3: safe HTTP security headers.
@app.middleware("http")
async def _security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    return response

'''
s = s[:m.end()] + "\n" + middleware + s[m.end():]

if "days must be between 1 and 365" not in s:
    old = '''def get_history(system_name: str, metric_name: str, days: int = 5):
    return read_metric_history(metric_name, days=days)
'''
    new = '''def get_history(system_name: str, metric_name: str, days: int = 5):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")
    return read_metric_history(metric_name, days=days)
'''
    s = s.replace(old, new, 1)

if 'detail="Unable to create system."' not in s:
    old = '''    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
'''
    new = '''    except Exception as e:
        log.error("Failed to create system: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Unable to create system.")
'''
    s = s.replace(old, new, 1)

APP.write_text(s, encoding="utf-8")
print("Milestone 8.3 repaired and applied safely.")

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"

if not APP.exists():
    raise SystemExit(f"Cannot find {APP}")

s = APP.read_text(encoding="utf-8")

backup = APP.with_suffix(".py.pre_8_3_final_repair.bak")
if not backup.exists():
    shutil.copy2(APP, backup)

# Remove the misplaced middleware by using its known start marker and
# the following FastAPI application declaration as the hard boundary.
marker = "# Milestone 8.3: safe HTTP security headers."
app_marker = 'app = FastAPI(\n    title="InfraBeatOps Dashboard",\n    version="1.0.0",\n    lifespan=lifespan,\n)\n'

if marker in s:
    start = s.index(marker)
    boundary = s.find(app_marker, start)
    if boundary == -1:
        raise RuntimeError("Found old 8.3 middleware but could not find the FastAPI app declaration.")
    s = s[:start] + s[boundary:]

# If a correctly placed copy already exists, remove it so this operation is
# deterministic, then insert exactly one copy after FastAPI initialization.
marker = "# Milestone 8.3: safe HTTP security headers."
while marker in s:
    start = s.index(marker)
    after = s.find("\n\n", start)
    if after == -1:
        raise RuntimeError("Could not remove existing 8.3 middleware block safely.")
    # The middleware block is immediately followed by a blank line.
    s = s[:start] + s[after + 2:]

if app_marker not in s:
    raise RuntimeError("FastAPI application declaration not found.")

middleware = (
    "\n# Milestone 8.3: safe HTTP security headers.\n"
    '@app.middleware("http")\n'
    "async def _security_headers_middleware(request, call_next):\n"
    "    response = await call_next(request)\n"
    '    response.headers.setdefault("X-Content-Type-Options", "nosniff")\n'
    '    response.headers.setdefault("X-Frame-Options", "DENY")\n'
    '    response.headers.setdefault("Referrer-Policy", "no-referrer")\n'
    "    response.headers.setdefault(\n"
    '        "Permissions-Policy",\n'
    '        "camera=(), microphone=(), geolocation=()",\n'
    "    )\n"
    "    return response\n"
)

s = s.replace(app_marker, app_marker + middleware, 1)

# Apply the two non-ordering security hardening changes if the endpoint
# shapes are present in this project.
history_old = (
    '@app.get("/api/history/{system_name}/{metric_name}")\n'
    "def get_history(system_name: str, metric_name: str, days: int = 5):\n"
    "    return read_metric_history(metric_name, days=days)\n"
)
history_new = (
    '@app.get("/api/history/{system_name}/{metric_name}")\n'
    "def get_history(system_name: str, metric_name: str, days: int = 5):\n"
    "    if days < 1 or days > 365:\n"
    '        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")\n'
    "    return read_metric_history(metric_name, days=days)\n"
)
if "days must be between 1 and 365" not in s and history_old in s:
    s = s.replace(history_old, history_new, 1)

old_exc = (
    "    except Exception as e:\n"
    "        raise HTTPException(status_code=500, detail=str(e))\n"
)
new_exc = (
    "    except Exception as e:\n"
    '        log.error("Failed to create system: %s", type(e).__name__)\n'
    '        raise HTTPException(status_code=500, detail="Unable to create system.")\n'
)
if old_exc in s:
    s = s.replace(old_exc, new_exc, 1)

APP.write_text(s, encoding="utf-8")
print("Milestone 8.3 repaired and applied safely.")

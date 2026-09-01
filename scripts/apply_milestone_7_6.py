from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"

if not APP.exists():
    raise SystemExit(f"dashboard/app.py not found: {APP}")

s = APP.read_text(encoding="utf-8")

if "from contextlib import asynccontextmanager" not in s:
    marker = "import threading\n"
    if marker not in s:
        raise RuntimeError("Could not find dashboard import section.")
    s = s.replace(marker, marker + "from contextlib import asynccontextmanager\n", 1)

old_app = 'app = FastAPI(title="InfraBeatOps Dashboard", version="1.0.0")\napp.include_router(intelligence_router)\n'
if old_app not in s:
    if "lifespan=lifespan" in s and '@app.on_event("startup")' not in s:
        print("Milestone 7.6 lifespan migration already present.")
        raise SystemExit(0)
    raise RuntimeError("Expected FastAPI app initialization was not found.")

new_app = """# Milestone 7.6: FastAPI lifespan lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preserve the existing scheduler startup behavior.
    start_scheduler()
    try:
        yield
    finally:
        # The scheduler thread remains daemonized, matching the old behavior.
        pass


app = FastAPI(
    title="InfraBeatOps Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(intelligence_router)
"""
s=s.replace(old_app,new_app,1)

old_decorator='@app.on_event("startup")\ndef start_scheduler():\n'
if old_decorator in s:
    s=s.replace(old_decorator,'def start_scheduler():\n',1)
elif '@app.on_event("startup")' in s:
    raise RuntimeError("Unexpected startup handler formatting; refusing blind edit.")

backup=APP.with_suffix(".py.milestone7_6.bak")
if not backup.exists():
    shutil.copy2(APP, backup)

APP.write_text(s,encoding="utf-8")
print("Milestone 7.6 applied safely.")
print("Backup:", backup)

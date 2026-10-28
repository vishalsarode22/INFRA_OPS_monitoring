from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "core" / "intelligence_runtime.py"
APP = ROOT / "dashboard" / "app.py"
MAIN = ROOT / "main.py"

for p in (RUNTIME, APP, MAIN):
    if not p.exists():
        raise SystemExit(f"Required file not found: {p}")

s = RUNTIME.read_text(encoding="utf-8")
if "Milestone 8.2" not in s:
    s = s.replace(
        "from core.models import MonitoringResult\n",
        "from core.models import MonitoringResult\nfrom utils.logger import get_logger\n",
        1,
    )
    s = s.replace(
        "from utils.paths import BASE_DIR\n",
        "from utils.paths import BASE_DIR\n\nlog = get_logger(__name__, \"intelligence\")\n",
        1,
    )
    old = "        self.persist_system_state(result.system)\n        return intelligence\n"
    new = '''        try:
            self.persist_system_state(result.system)
        except Exception as exc:
            # Persistence is valuable but must never turn a successful
            # monitoring cycle into an application failure.
            log.error(
                "Milestone 8.2: intelligence state persistence failed for %s: %s",
                result.system,
                type(exc).__name__,
            )
        return intelligence
'''
    if old not in s:
        raise SystemExit("Could not find intelligence persistence call")
    s = s.replace(old, new, 1)
    RUNTIME.write_text(s, encoding="utf-8")

s = APP.read_text(encoding="utf-8")
if "Milestone 8.2" not in s:
    old = '''def _run_single_system(system_config: dict):
    from main import run_pipeline_for_system
    _run_state["current_system"] = system_config["name"]
    try:
        run_pipeline_for_system(system_config)
        _append_run_history({
            "system": system_config["name"],
            "event": "Sweep completed",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ok",
        })
    except Exception as e:
        log.error(f"Pipeline error for {system_config['name']}: {e}")
        _append_run_history({
            "system": system_config["name"],
            "event": f"Sweep failed: {e}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error",
        })
    _run_state["current_system"] = None
'''
    new = '''def _run_single_system(system_config: dict):
    """Run one system without allowing its failure to poison the scheduler."""
    from main import run_pipeline_for_system
    name = system_config["name"]
    _run_state["current_system"] = name
    try:
        ok = bool(run_pipeline_for_system(system_config))
        _append_run_history({
            "system": name,
            "event": "Sweep completed" if ok else "Sweep failed",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ok" if ok else "error",
        })
        return ok
    except Exception as e:
        log.error("Pipeline error for %s: %s", name, type(e).__name__)
        _append_run_history({
            "system": name,
            "event": f"Sweep failed: {type(e).__name__}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "error",
        })
        return False
    finally:
        # Never leave the dashboard claiming a system is running after an
        # exception, timeout, or unexpected return.
        _run_state["current_system"] = None

# Milestone 8.2: per-system failures are isolated; remaining systems continue.
'''
    if old not in s:
        raise SystemExit("Could not find dashboard system runner")
    s = s.replace(old, new, 1)
    old2 = '''        systems = get_systems()
        for sysconf in systems:
            _run_single_system(sysconf)
        _run_state["error"] = None
'''
    new2 = '''        systems = get_systems()
        failures = 0
        for sysconf in systems:
            if not _run_single_system(sysconf):
                failures += 1
        _run_state["error"] = (
            f"{failures} system(s) failed during the sweep." if failures else None
        )
'''
    if old2 not in s:
        raise SystemExit("Could not find dashboard sweep loop")
    s = s.replace(old2, new2, 1)
    APP.write_text(s, encoding="utf-8")

s = MAIN.read_text(encoding="utf-8")
if "Milestone 8.2" not in s:
    old = '''    except Exception as e:
        log.error(f"AI/intelligence analysis failed after final metric collection: {e}")
        result.errors.append(f"ai_analyzer: {e}")
    except Exception as e:
        log.error(f"AI analysis failed after final metric collection: {e}")
        result.errors.append(f"ai_analyzer: {e}")
'''
    new = '''    except Exception as e:
        # AI/intelligence is optional; deterministic monitoring and incident
        # severity remain available when the provider fails.
        log.error(
            "Milestone 8.2: AI/intelligence analysis failed after final metric collection: %s",
            type(e).__name__,
        )
        result.errors.append(f"ai_analyzer: {type(e).__name__}")
'''
    if old not in s:
        raise SystemExit("Could not find duplicate AI exception block")
    s = s.replace(old, new, 1)
    MAIN.write_text(s, encoding="utf-8")

print("Milestone 8.2 applied safely.")

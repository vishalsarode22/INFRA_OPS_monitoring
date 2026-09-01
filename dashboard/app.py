"""
Dashboard backend: serves multi-system status, trend history, manages
system configuration, and runs a background scheduler that automatically
monitors all configured systems on an interval -- no manual trigger needed.

Also serves:
    - a lightweight run-history feed (recent sweep completions/failures)
    - the list of configured T-code checks (for the Check Registry view)
    - generated report files per system (for the Evidence Reports view),
      served statically under /reports/...
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import threading
from contextlib import asynccontextmanager
import time as time_module
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.status_snapshot import load_snapshot, list_snapshot_systems
from dashboard.intelligence_api import router as intelligence_router
from core.config_loader import (
    get_systems, get_systems_safe, add_system, delete_system, get_monitoring_tasks,
    get_scheduler_interval_minutes, set_scheduler_interval_minutes,
)
from core.production_health import liveness, readiness
from reporting.history_reader import read_metric_history
from utils.logger import get_logger

log = get_logger(__name__, "dashboard")

# Milestone 7.6: FastAPI lifespan lifecycle
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

# Milestone 8.3: safe HTTP security headers.
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

app.include_router(intelligence_router)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_history.json")

SCHEDULE_INTERVAL_MINUTES = get_scheduler_interval_minutes(default=120)

_run_state = {"running": False, "error": None, "current_system": None,
              "cancel_requested": False, "stopped_after": None}
_run_lock = threading.Lock()
_run_history_lock = threading.Lock()
_scheduler_state = {"last_cycle_start": None, "next_run_at": None, "enabled": True,
                    "snooze_until": None}


@app.get("/healthz")
def healthz():
    """Process liveness endpoint. Does not require SAP/AI connectivity."""
    return liveness()


@app.get("/readyz")
def readyz():
    """Local readiness endpoint with safe, non-secret diagnostics."""
    result = readiness()
    if result["status"] != "ready":
        # Keep the payload useful while letting load balancers distinguish a
        # live-but-not-ready service.
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=result)
    return result


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/status/{system_name}")
def get_status(system_name: str):
    snapshot = load_snapshot(system_name)
    if snapshot is None:
        return {"available": False}
    return {"available": True, **snapshot}


@app.get("/api/status")
def get_status_default():
    systems = get_systems()
    if not systems:
        return {"available": False}
    return get_status(systems[0]["name"])


# ---------------------------------------------------------------------------
# Live RFC wall display
# ---------------------------------------------------------------------------
#
# These endpoints do a FRESH RFC read and are safe to poll every few seconds.
# They never trigger a sweep: a sweep relaunches SAP Logon and would kill the
# operator's own GUI session on the monitoring machine.

@app.get("/api/live/{system_name}")
def get_live(system_name: str):
    from collectors.rfc_live import read_live, attach_snapshot_extras

    cfg = next((s for s in get_systems() if s.get("name") == system_name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    payload = read_live(system_name, cfg)
    return attach_snapshot_extras(payload, load_snapshot(system_name))


@app.get("/api/live")
def get_live_all():
    """Every configured system, for the wall display."""
    from collectors.rfc_live import read_live, attach_snapshot_extras

    out = []
    for cfg in get_systems():
        name = cfg.get("name", "?")
        try:
            payload = read_live(name, cfg)
            out.append(attach_snapshot_extras(payload, load_snapshot(name)))
        except Exception as exc:
            # One unreachable system must not blank the whole wall.
            out.append({"system": name, "connected": False,
                        "error": f"{type(exc).__name__}: {exc}"})

    live = sum(1 for s in out if s.get("connected"))
    return {
        "systems": out,
        "live_count": live,
        "total": len(out),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Fleet overview
# ---------------------------------------------------------------------------

# Which metric feeds each tile on a system card. Names are the canonical ones
# shared by the SSH, RFC and SAP GUI collectors, so a tile fills from
# whichever collector reached the system.
_CARD_TILES = {
    "cpu":         ["cpu"],
    "memory":      ["memory"],
    "dispatcher":  ["sap_process_disp+work"],
    "icm":         ["sap_process_icman"],
    "gateway":     ["sap_process_gwrd"],
    "workprocess": ["sap.sm66.wp_saturation_pct", "sap.sm66.running_processes"],
    "smlg":        ["sap.smlg.response_time"],
}

_SEVERITY_RANK = {"UNKNOWN": 0, "NORMAL": 1, "WARNING": 2, "CRITICAL": 3}


@app.get("/api/overview")
def get_overview():
    """
    Fleet summary for the Overview page: one card per configured system plus
    headline counts.

    A system with no snapshot, or a snapshot that collected nothing, reports
    UNKNOWN -- never healthy. "We could not reach it" and "it is fine" are
    different answers and the UI must be able to tell them apart.
    """
    cards, counts = [], {"total": 0, "healthy": 0, "warning": 0, "critical": 0, "unknown": 0}

    for cfg in get_systems():
        name = cfg.get("name", "?")
        counts["total"] += 1
        snapshot = load_snapshot(name) or {}
        metrics = {m.get("name"): m for m in snapshot.get("metrics", []) or []}

        status = (snapshot.get("overall_status") or "UNKNOWN").upper()
        if not metrics:
            status = "UNKNOWN"

        tiles = {}
        for tile, candidates in _CARD_TILES.items():
            hit = next((metrics[c] for c in candidates if c in metrics), None)
            tiles[tile] = {
                "value": hit.get("display_value") if hit else "No data",
                "status": (hit.get("status") if hit else "UNKNOWN") or "UNKNOWN",
                "known": bool(hit),
            }

        severity = {"CRITICAL": 0, "WARNING": 0, "UNKNOWN": 0}
        for m in metrics.values():
            key = (m.get("status") or "UNKNOWN").upper()
            if key in severity:
                severity[key] += 1

        # Which collection paths this system is configured to use, so the UI
        # can say WHY a system has no data rather than just showing blanks.
        paths = []
        if cfg.get("rfc"):
            paths.append("RFC")
        if cfg.get("has_os_access"):
            paths.append("SSH")
        if cfg.get("has_gui_access"):
            paths.append("GUI")

        cards.append({
            "name": name,
            "sid": cfg.get("sap_system_id") or name,
            "client": cfg.get("client", ""),
            "host": (cfg.get("rfc") or {}).get("ashost") or cfg.get("ssh_host") or "",
            "status": status,
            "tiles": tiles,
            "critical": severity["CRITICAL"],
            "warning": severity["WARNING"],
            "unknown": severity["UNKNOWN"],
            "metric_count": len(metrics),
            "paths": paths,
            "last_scan": snapshot.get("cycle_timestamp") or snapshot.get("generated_at") or "",
            "incidents": len(snapshot.get("incidents") or []),
            "events": len(snapshot.get("events") or []),
        })

        bucket = {"NORMAL": "healthy", "WARNING": "warning",
                  "CRITICAL": "critical"}.get(status, "unknown")
        counts[bucket] += 1

    cards.sort(key=lambda c: (-_SEVERITY_RANK.get(c["status"], 0), c["name"]))

    return {
        "counts": counts,
        "systems": cards,
        "incidents_open": sum(c["incidents"] for c in cards),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
#
# Separate documents rather than one page with tabs: each screen answers a
# different question, loads only what it needs, and can be bookmarked or put
# on a wall display on its own.

def _page(name: str):
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Page not built: {name}")
    return FileResponse(path)


@app.get("/overview")
def overview_page():
    return _page("overview.html")


@app.get("/wall")
def wall_page():
    """Live RFC wall display for the NOC monitor."""
    return _page("wall.html")


@app.get("/system")
def system_page():
    return _page("system.html")


@app.get("/systems-page")
def systems_list_page():
    return _page("systems.html")


@app.get("/reports-page")
def reports_page():
    return _page("reports.html")


@app.get("/api/report/{system_name}")
def get_health_report(system_name: str):
    """
    Findings, recommended solutions and a health summary for one system.

    Built from the last completed sweep, not from a live read: a report needs
    the full metric set the sweep produces, and rebuilding it on every request
    would hammer the SAP system.
    """
    from core.status_snapshot import load_snapshot as _load
    from evaluation.health_report import build_report
    from core.models import MonitoringResult, MetricResult, Status
    from datetime import datetime as _dt

    snapshot = _load(system_name)
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"No monitoring snapshot for {system_name}. Run a sweep first.")

    stamp = snapshot.get("cycle_timestamp") or snapshot.get("generated_at")
    try:
        cycle = _dt.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        cycle = _dt.now()

    result = MonitoringResult(
        system=snapshot.get("system", system_name),
        client=snapshot.get("client", ""),
        cycle_timestamp=cycle,
    )
    for m in snapshot.get("metrics", []) or []:
        try:
            status = Status(str(m.get("status", "UNKNOWN")).upper())
        except ValueError:
            status = Status.UNKNOWN
        result.metrics.append(MetricResult(
            name=m.get("name", "?"),
            value=None,
            display_value=str(m.get("display_value", "")),
            status=status,
            source=m.get("source", ""),
            tcode=m.get("tcode"),
            detail=m.get("detail", "") or "",
        ))
    result.compute_overall_status()

    return build_report(result)


@app.get("/api/history/{system_name}/{metric_name}")
def get_history(system_name: str, metric_name: str, days: int = 5):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")
    return read_metric_history(metric_name, days=days)


@app.get("/api/systems")
def list_systems():
    return get_systems_safe()


class NewSystem(BaseModel):
    name: str
    client: str
    # GUI is now OPTIONAL. A system reachable only over RFC -- which is the
    # normal case for RISE and hosted systems -- must be addable without
    # inventing SAP Logon Pad details it does not have.
    connection_name: str = ""
    username: str = ""
    password: str = ""
    language: str = "EN"
    has_gui_access: bool = False
    has_os_access: bool = False
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_username: str = ""
    ssh_password: str = ""
    sap_instance_nr: str = ""
    sap_system_id: str = ""
    # Without these the RFC collector never runs for the system: no T-code
    # counters, nothing on the live wall.
    rfc_ashost: str = ""
    rfc_sysnr: str = ""
    rfc_client: str = ""
    rfc_username: str = ""
    rfc_password: str = ""
    rfc_saprouter: str = ""
    # Per-system alerting. Blank fields inherit the global .env settings, so
    # a single-tenant setup needs none of these.
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_username: str = ""
    smtp_password: str = ""
    sender_email: str = ""
    alert_receivers: str = ""
    alert_receivers_cc: str = ""
    monitor_now: bool = True


@app.get("/api/systems/{system_name}/config")
def get_system_config(system_name: str):
    """
    One system's settings, for pre-filling the edit form.

    Passwords are returned as empty strings with a `has_*` flag alongside, so
    the form can show "leave blank to keep" rather than echoing the secret
    back into a browser. Submitting a blank password preserves the stored one.
    """
    cfg = next((s for s in get_systems() if s.get("name") == system_name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    rfc = cfg.get("rfc") or {}
    return {
        "name": cfg.get("name", ""),
        "sap_system_id": cfg.get("sap_system_id", ""),
        "client": cfg.get("client", ""),
        "language": cfg.get("language", "EN"),

        "has_gui_access": bool(cfg.get("has_gui_access")),
        "connection_name": cfg.get("connection_name", ""),
        "username": cfg.get("username", ""),
        "has_password": bool(cfg.get("password")),

        "has_os_access": bool(cfg.get("has_os_access")),
        "ssh_host": cfg.get("ssh_host", ""),
        "ssh_port": cfg.get("ssh_port", 22),
        "ssh_username": cfg.get("ssh_username", ""),
        "has_ssh_password": bool(cfg.get("ssh_password")),
        "sap_instance_nr": cfg.get("sap_instance_nr", ""),

        "rfc_ashost": rfc.get("ashost", ""),
        "rfc_sysnr": rfc.get("sysnr", ""),
        "rfc_client": rfc.get("client", ""),
        "rfc_username": rfc.get("username", ""),
        "has_rfc_password": bool(rfc.get("password")),
        "rfc_saprouter": rfc.get("saprouter", ""),

        "smtp_host": cfg.get("smtp_host", ""),
        "smtp_port": cfg.get("smtp_port", ""),
        "smtp_username": cfg.get("smtp_username", ""),
        "has_smtp_password": bool(cfg.get("smtp_password")),
        "sender_email": cfg.get("sender_email", ""),
        "alert_receivers": cfg.get("alert_receivers", ""),
        "alert_receivers_cc": cfg.get("alert_receivers_cc", ""),
    }


@app.post("/api/systems")
def create_system(system: NewSystem):
    try:
        # NOTE: always call with keyword arguments here -- add_system() has
        # many same-typed parameters in a row (ssh_host, ssh_port, ...), and
        # a positional call previously shifted ssh_host into the
        # has_gui_access slot, silently corrupting every system added.
        add_system(
            name=system.name, client=system.client,
            connection_name=system.connection_name,
            username=system.username, password=system.password,
            language=system.language,
            has_gui_access=system.has_gui_access, has_os_access=system.has_os_access,
            ssh_host=system.ssh_host, ssh_port=system.ssh_port,
            ssh_username=system.ssh_username, ssh_password=system.ssh_password,
            sap_instance_nr=system.sap_instance_nr,
            sap_system_id=system.sap_system_id,
            rfc_ashost=system.rfc_ashost, rfc_sysnr=system.rfc_sysnr,
            rfc_client=system.rfc_client, rfc_username=system.rfc_username,
            rfc_password=system.rfc_password, rfc_saprouter=system.rfc_saprouter,
            smtp_host=system.smtp_host, smtp_port=system.smtp_port,
            smtp_username=system.smtp_username, smtp_password=system.smtp_password,
            sender_email=system.sender_email,
            alert_receivers=system.alert_receivers,
            alert_receivers_cc=system.alert_receivers_cc,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not add system: {exc}")

    # New credentials deserve an immediate retry rather than inheriting a
    # cooldown left over from a previous failure for the same name.
    try:
        from collectors.rfc_collector import clear_cooldown
        clear_cooldown(system.name)
    except Exception:
        pass

    warnings = []
    if not (system.rfc_ashost and system.rfc_username and system.rfc_password):
        warnings.append(
            "No RFC details supplied, so this system will produce no T-code "
            "counters and nothing on the live wall. Add an RFC host, user and "
            "password to collect without SAP GUI or OS access.")
    if system.has_gui_access and not system.connection_name:
        warnings.append(
            "GUI access is enabled but no SAP Logon connection name was given; "
            "GUI evidence will fail until one is set.")

    started = False
    if system.monitor_now and not _run_state["running"]:
        thread = threading.Thread(target=_run_one_system_background,
                                  args=(system.name,), daemon=True)
        thread.start()
        started = True

    return {"success": True, "monitoring_started": started, "warnings": warnings}


@app.delete("/api/systems/{system_name}")
def remove_system(system_name: str):
    removed = delete_system(system_name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"System '{system_name}' not found.")
    return {"success": True}


class SchedulerSettings(BaseModel):
    interval_minutes: int


@app.get("/api/scheduler")
def scheduler_status():
    """Whether automatic monitoring is on, and when it next runs."""
    snooze = _scheduler_state.get("snooze_until")
    snoozed = bool(snooze and datetime.now() < snooze)
    if snooze and not snoozed:
        # Expired snooze clears itself, so the UI never shows a stale one.
        _scheduler_state["snooze_until"] = None

    return {
        "enabled": bool(_scheduler_state["enabled"]) and not snoozed,
        "paused": not bool(_scheduler_state["enabled"]),
        "snoozed": snoozed,
        "snooze_until": snooze.strftime("%Y-%m-%d %H:%M") if snoozed else None,
        "interval_minutes": SCHEDULE_INTERVAL_MINUTES,
        "next_run_at": None if (snoozed or not _scheduler_state["enabled"])
                       else _scheduler_state["next_run_at"],
        "last_cycle_start": _scheduler_state["last_cycle_start"],
        "running": bool(_run_state["running"]),
        "current_system": _run_state.get("current_system"),
    }


@app.post("/api/scheduler/snooze")
def scheduler_snooze(minutes: int = 60):
    """
    Suppress automatic monitoring for a period, then resume by itself.

    A plain pause is easy to forget: monitoring stays off for days and nobody
    remembers switching it off. A snooze states when it ends, so the default
    outcome is monitoring coming back rather than staying silent.

    Use it when you need the desktop for a while -- a demo, a deployment, or
    working in SAP GUI yourself.
    """
    try:
        minutes = max(1, min(int(minutes), 60 * 24 * 30))   # cap at 30 days
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="minutes must be a number")

    until = datetime.now() + timedelta(minutes=minutes)
    _scheduler_state["snooze_until"] = until
    _scheduler_state["next_run_at"] = None
    log.info(f"Automatic monitoring snoozed for {minutes} min (until {until:%Y-%m-%d %H:%M}).")

    return {
        "snoozed": True,
        "minutes": minutes,
        "until": until.strftime("%Y-%m-%d %H:%M"),
        "note": "Automatic monitoring resumes by itself at that time.",
    }


@app.post("/api/scheduler/{action}")
def scheduler_control(action: str):
    """
    Pause or resume automatic monitoring.

    Pausing does NOT abort a run already in progress -- killing a sweep
    mid-way would leave SAP Logon open and a half-written snapshot behind.
    It stops the NEXT cycle from starting, which is what an operator means
    when a sweep is fighting them for the desktop.

    This is in-memory only: a restart returns to the MONITOR_ON_STARTUP /
    SCHEDULER_ENABLED settings in .env. That is deliberate -- a pause is a
    temporary act, and silently persisting it would leave monitoring off for
    weeks with nobody remembering why.
    """
    action = action.strip().lower()
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=400,
                            detail="Use /api/scheduler/pause or /api/scheduler/resume.")

    _scheduler_state["enabled"] = (action == "resume")
    # Clear any snooze: leaving one in place would mean "Resume" appeared to
    # work while nothing actually ran until the snooze expired.
    _scheduler_state["snooze_until"] = None
    log.info(f"Automatic monitoring {'resumed' if action == 'resume' else 'paused'} "
             f"from the dashboard.")

    return {
        "enabled": _scheduler_state["enabled"],
        "running": bool(_run_state["running"]),
        "note": ("A run already in progress will finish."
                 if _run_state["running"] and action == "pause" else ""),
        "next_run_at": _scheduler_state["next_run_at"] if _scheduler_state["enabled"] else None,
    }


@app.get("/api/schedules")
def get_all_schedules():
    """Every configured system with its schedule, description and next run."""
    from core.schedules import load_schedules, describe, next_run_at, DEFAULT

    stored = load_schedules()
    out = []
    for cfg in get_systems():
        name = cfg.get("name")
        sched = stored.get(name) or dict(DEFAULT)

        snap = load_snapshot(name) or {}
        last = snap.get("cycle_timestamp")
        last_dt = None
        if last:
            try:
                last_dt = datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

        nxt = next_run_at(sched, last_dt)
        out.append({
            "system": name,
            "sid": cfg.get("sap_system_id") or name,
            "schedule": sched,
            "description": describe(sched),
            "last_run": last,
            "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
        })

    return {
        "systems": out,
        "scheduler_enabled": bool(_scheduler_state["enabled"]),
        "running": bool(_run_state["running"]),
        "current_system": _run_state.get("current_system"),
    }


class ScheduleUpdate(BaseModel):
    enabled: bool = True
    mode: str = "hours"          # minutes | hours | daily
    every: int = 2
    at: str = "08:00"
    days: list[str] = []


@app.post("/api/schedules/{system_name}")
def update_schedule(system_name: str, body: ScheduleUpdate):
    from core.schedules import set_schedule, describe, next_run_at

    if not any(s.get("name") == system_name for s in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    saved = set_schedule(system_name, body.dict())
    log.info(f"Schedule updated for {system_name}: {describe(saved)}")

    snap = load_snapshot(system_name) or {}
    last_dt = None
    if snap.get("cycle_timestamp"):
        try:
            last_dt = datetime.strptime(str(snap["cycle_timestamp"]), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    nxt = next_run_at(saved, last_dt)

    return {
        "success": True,
        "schedule": saved,
        "description": describe(saved),
        "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
    }


@app.get("/scheduler")
def scheduler_page():
    return _page("scheduler.html")


@app.get("/api/scheduler-settings")
def get_scheduler_settings():
    return {"interval_minutes": SCHEDULE_INTERVAL_MINUTES}


@app.post("/api/scheduler-settings")
def update_scheduler_settings(settings: SchedulerSettings):
    global SCHEDULE_INTERVAL_MINUTES
    if settings.interval_minutes < 5:
        raise HTTPException(status_code=400, detail="Interval must be at least 5 minutes.")
    SCHEDULE_INTERVAL_MINUTES = settings.interval_minutes
    set_scheduler_interval_minutes(settings.interval_minutes)
    log.info(f"Scheduler interval updated to {SCHEDULE_INTERVAL_MINUTES} minutes "
             f"(takes effect after the current wait cycle completes).")
    return {"success": True, "interval_minutes": SCHEDULE_INTERVAL_MINUTES}


@app.get("/api/tcodes")
def list_tcodes():
    """Returns the configured T-code check list, for the Check Registry view."""
    try:
        return get_monitoring_tasks()
    except Exception as e:
        log.error(f"Failed to load monitoring tasks: {e}")
        return []


@app.get("/api/reports/{system_name}")
def list_reports(system_name: str):
    """
    Returns recent report file links for a system (PDF + Excel), served
    statically under /reports/YYYY-MM-DD/filename. Used by the
    Evidence Reports view.
    """
    if not os.path.isdir(REPORTS_DIR):
        return []
    day_dirs = sorted(os.listdir(REPORTS_DIR), reverse=True)
    results = []
    for day in day_dirs[:7]:
        day_path = os.path.join(REPORTS_DIR, day)
        if not os.path.isdir(day_path):
            continue
        for fname in os.listdir(day_path):
            if not fname.lower().endswith((".pdf", ".xlsx")):
                continue
            if system_name.lower() not in fname.lower():
                continue
            results.append({"date": day, "filename": fname, "url": f"/reports/{day}/{fname}"})
    return results[:10]


def _append_run_history(event: dict):
    with _run_history_lock:
        history = []
        if os.path.exists(RUN_HISTORY_PATH):
            try:
                with open(RUN_HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(event)
        history = history[-50:]
        try:
            with open(RUN_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log.error(f"Failed to write run history: {e}")


@app.get("/api/run-history")
def get_run_history():
    if not os.path.exists(RUN_HISTORY_PATH):
        return []
    try:
        with open(RUN_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(reversed(data))[:20]
    except Exception:
        return []


def _run_single_system(system_config: dict):
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


def _run_all_systems_background():
    with _run_lock:
        if _run_state["running"]:
            return
        _run_state["running"] = True
        _run_state["error"] = None
        _run_state["cancel_requested"] = False
        _run_state["stopped_after"] = None

    try:
        systems = get_systems()
        failures = 0
        stopped = None

        for sysconf in systems:
            # Cancellation is checked BETWEEN systems, not inside one.
            #
            # A sweep drives SAP GUI: it launches SAP Logon, logs in, opens
            # T-codes and captures screenshots. Killing that thread mid-way
            # would leave an orphaned SAP Logon process, a half-written
            # snapshot and possibly a stuck GUI session. Finishing the system
            # in flight and stopping before the next one is the safe point,
            # and it is what an operator means by "stop" -- give me my
            # desktop back and do not start another.
            if _run_state["cancel_requested"]:
                stopped = sysconf.get("name")
                log.info(f"Sweep stopped by request before {stopped}.")
                break

            if not _run_single_system(sysconf):
                failures += 1

        if stopped:
            remaining = [s.get("name") for s in systems]
            idx = remaining.index(stopped) if stopped in remaining else 0
            _run_state["stopped_after"] = stopped
            _run_state["error"] = (
                f"Sweep stopped by request. {len(remaining) - idx} system(s) "
                f"not monitored: {', '.join(remaining[idx:])}."
            )
        else:
            _run_state["error"] = (
                f"{failures} system(s) failed during the sweep." if failures else None
            )
    except Exception as e:
        _run_state["error"] = str(e)
    finally:
        _run_state["running"] = False
        _run_state["cancel_requested"] = False


@app.post("/api/run-stop")
def stop_run():
    """
    Stop monitoring: the current sweep and everything scheduled after it.

    Two separate effects, because "stop" means both to an operator:
      1. the sweep in progress stops after the system it is currently on;
      2. automatic monitoring is paused, so nothing starts again on its own.

    The system in flight is allowed to finish. Aborting it mid-way would
    leave SAP Logon open, a half-written snapshot on disk and possibly a
    stuck GUI session -- a worse state than the one being escaped.

    Resume automatic monitoring with the Resume button or
    POST /api/scheduler/resume.
    """
    was_running = bool(_run_state["running"])
    current = _run_state.get("current_system")

    _run_state["cancel_requested"] = True
    _scheduler_state["enabled"] = False
    _scheduler_state["next_run_at"] = None

    log.info(
        f"Stop requested from the dashboard. "
        f"{'Finishing ' + str(current) + ' then stopping.' if was_running else 'No sweep was running.'} "
        f"Automatic monitoring paused."
    )

    return {
        "stopping": was_running,
        "current_system": current,
        "scheduler_paused": True,
        "note": (
            f"Finishing {current} then stopping — a sweep cannot be aborted "
            f"mid-system without leaving SAP Logon open."
            if was_running else
            "No sweep was running. Automatic monitoring is now paused."
        ),
    }


@app.post("/api/run-now")
def run_now():
    if _run_state["running"]:
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    thread = threading.Thread(target=_run_all_systems_background, daemon=True)
    thread.start()
    return {"started": True, "scope": "all"}


def _run_one_system_background(system_name: str):
    """Runs the full pipeline for a single system, then clears run state."""
    with _run_lock:
        if _run_state["running"]:
            return
        _run_state["running"] = True
        _run_state["error"] = None
        _run_state["cancel_requested"] = False
        _run_state["current_system"] = system_name
    try:
        cfg = next((s for s in get_systems() if s.get("name") == system_name), None)
        if cfg is None:
            _run_state["error"] = f"Unknown system: {system_name}"
            return
        _run_single_system(cfg)
    except Exception as exc:
        log.error("Single-system run failed for %s: %s", system_name, exc)
        _run_state["error"] = str(exc)
    finally:
        with _run_lock:
            _run_state["running"] = False
            _run_state["current_system"] = None


@app.post("/api/run-now/{system_name}")
def run_now_single(system_name: str):
    """
    Monitor ONE system.

    A full sweep drives SAP GUI for every configured system and takes
    minutes; when an operator wants to re-check the system they are actually
    looking at, running all of them is the wrong tool. It also means a
    problem on one system no longer forces a wait on three others.
    """
    if _run_state["running"]:
        raise HTTPException(
            status_code=409,
            detail=f"A run is already in progress ({_run_state.get('current_system') or 'all systems'}).")

    if not any(s.get("name") == system_name for s in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    thread = threading.Thread(target=_run_one_system_background,
                              args=(system_name,), daemon=True)
    thread.start()
    return {"started": True, "scope": system_name}


@app.get("/api/run-status")
def run_status():
    return {**_run_state, **_scheduler_state}


def _scheduler_loop():
    """
    Per-system monitoring schedule.

    Each system has its own cadence (see core/schedules.py). The loop wakes
    every 20 seconds, asks which systems are due, and runs them one at a
    time -- never concurrently, because a sweep drives SAP GUI and two at
    once would fight over the same desktop.

    Startup behaviour is governed by .env:
        MONITOR_ON_STARTUP=false   (default) no sweep when the server starts
        SCHEDULER_ENABLED=false    never run automatically
    """
    from core.schedules import load_schedules, is_due, next_run_at, describe

    def _flag(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        return default if raw is None else str(raw).strip().lower() in ("1", "true", "yes", "on")

    if not _flag("SCHEDULER_ENABLED", True):
        log.info("Scheduler disabled (SCHEDULER_ENABLED=false). Manual runs only.")
        _scheduler_state["enabled"] = False
        return

    # last_run is seeded from each system's snapshot, so a restart does not
    # re-run everything that was already monitored a few minutes ago.
    last_run: dict[str, datetime] = {}
    for cfg in get_systems():
        name = cfg.get("name")
        snap = load_snapshot(name) or {}
        stamp = snap.get("cycle_timestamp")
        if stamp:
            try:
                last_run[name] = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

    if not _flag("MONITOR_ON_STARTUP", False):
        # Treat startup as "just ran" for systems with no history, so a
        # restart never triggers an immediate sweep the operator did not ask
        # for. Systems with history keep their real last-run time.
        for cfg in get_systems():
            last_run.setdefault(cfg.get("name"), datetime.now())
        log.info("Scheduler: no run on startup. Systems will run on their own schedules.")
    else:
        log.info("Scheduler: MONITOR_ON_STARTUP=true -- due systems will run shortly.")

    while True:
        try:
            snooze = _scheduler_state.get("snooze_until")
            snoozed = bool(snooze and datetime.now() < snooze)
            if snooze and not snoozed:
                _scheduler_state["snooze_until"] = None
                log.info("Snooze expired -- automatic monitoring resumed.")

            if _scheduler_state["enabled"] and not snoozed and not _run_state["running"]:
                schedules = load_schedules()
                upcoming = []

                for cfg in get_systems():
                    name = cfg.get("name")
                    sched = schedules.get(name) or {}
                    if not sched.get("enabled", True):
                        continue

                    if is_due(sched, last_run.get(name)):
                        log.info(f"Scheduler: {name} is due ({describe(sched)}).")
                        _scheduler_state["last_cycle_start"] = \
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        last_run[name] = datetime.now()
                        _run_one_system_background(name)
                        # One system per pass: re-evaluate afterwards so a
                        # long sweep cannot cause a pile-up of due systems.
                        break

                    nxt = next_run_at(sched, last_run.get(name))
                    if nxt:
                        upcoming.append(nxt)

                _scheduler_state["next_run_at"] = (
                    min(upcoming).strftime("%Y-%m-%d %H:%M:%S") if upcoming else None
                )
        except Exception as exc:
            # The scheduler must never die: a bad schedule entry would
            # otherwise stop all automatic monitoring until someone noticed.
            log.error(f"Scheduler pass failed: {type(exc).__name__}: {exc}")

        # Short sleep so a pause from the UI takes effect within seconds
        # rather than at the end of a multi-hour interval.
        time_module.sleep(20)


def start_scheduler():
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
    log.info(f"Auto-scheduler started: every {SCHEDULE_INTERVAL_MINUTES} minutes.")


if os.path.isdir(REPORTS_DIR):
    app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
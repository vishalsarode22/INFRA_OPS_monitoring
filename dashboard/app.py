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
import secrets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import threading
from contextlib import asynccontextmanager
import time as time_module
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Header, Depends
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


# ---------------------------------------------------------------------------
# Background live-read refresher
#
# Every RFC call in this app -- SM50/SM12/ST22/ST03/etc over the network to
# a real SAP gateway -- previously ran INSIDE the HTTP request handler for
# /api/live and /api/live/{system}. With four systems polled by every open
# tab (wall every 8s, Sky every 10s, a system page every 10s), an RFC read
# was in flight almost continuously. If the NetWeaver RFC SDK binding does
# not release the GIL for the duration of a blocking call -- common for C
# extensions wrapping synchronous network I/O without an explicit
# Py_BEGIN_ALLOW_THREADS -- the entire Python process stalls for that call's
# duration, including serving a plain static HTML file on an unrelated page.
# That would explain "every page is slow", not just the ones reading RFC.
#
# The fix does not require knowing pyrfc's exact GIL behaviour: move every
# RFC read out of the request path into ONE background loop, and have every
# HTTP handler read a dict lookup instead. Page loads become independent of
# RFC latency by construction. Staleness is bounded by _LIVE_REFRESH_S,
# which is the same order as the polling cadence it replaces.
# ---------------------------------------------------------------------------

_LIVE_REFRESH_S = int(os.environ.get("IBO_LIVE_REFRESH_SECONDS", "60") or 60)

# Ceiling on one refresh pass. pool.map() waits for every system before the
# loop sleeps, so without this a single host that neither answers nor resets
# the connection sets the cadence for all of them. A pass that overruns is
# abandoned; the systems that did answer have already written their entries
# into _live_cache, so nothing collected is thrown away.
_LIVE_PASS_TIMEOUT_S = int(os.environ.get("IBO_LIVE_PASS_TIMEOUT", "45") or 45)

_live_cache: dict[str, dict] = {}          # system name -> payload
_live_cache_at: dict[str, float] = {}      # system name -> monotonic time of that read
_live_cache_lock = threading.Lock()
_live_refresher_started = False


_rfc_pool = None
_rfc_pool_lock = threading.Lock()


def _get_rfc_pool():
    """
    Worker PROCESSES for RFC reads, created lazily.

    The middleware timing proved the whole interpreter freezes during RFC
    activity (a static JPEG took 30s, /api/live -- a dict copy -- took 62s,
    both spanning RFC bursts): pyrfc holds the GIL for the duration of its
    calls on this build. Threads share that GIL; processes do not. Reads run
    out-of-process and only the finished payload crosses back.

    Workers are long-lived so rfc_live's caches (perf/deep TTLs, SQLM-off,
    cooldowns) keep working inside them. IBO_RFC_PROCESSES=0 restores the
    old in-process behaviour if the pool ever misbehaves.
    """
    global _rfc_pool
    n = int(os.environ.get("IBO_RFC_PROCESSES", "2") or 2)
    if n <= 0:
        return None
    with _rfc_pool_lock:
        if _rfc_pool is None:
            from concurrent.futures import ProcessPoolExecutor
            _rfc_pool = ProcessPoolExecutor(max_workers=n)
        return _rfc_pool


def _running_under_pytest() -> bool:
    """
    True while pytest is driving this process.

    A test that boots the app through TestClient runs the REAL lifespan,
    which starts the scheduler and the live refresher. With
    IBO_LIVE_AUTO_REFRESH=1 set in .env that meant every `pytest` run
    opened RFC connections to every configured system -- PRD included --
    and read from them. Live readings appeared in test output, runs took as
    long as the SAP round trips, and the daemon threads kept logging after
    pytest had closed its streams, which is the "ValueError: I/O operation
    on closed file" traceback at the end of a run.

    A test suite must never touch a production system as a side effect of
    importing the app. Background work is therefore skipped under pytest;
    everything the tests actually assert -- routes, auth, contracts -- runs
    exactly as before.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def _refresh_one_live(cfg: dict) -> None:
    name = cfg.get("name", "?")
    try:
        pool = _get_rfc_pool()
        if pool is not None:
            from collectors.live_job import read_live_job
            payload = pool.submit(read_live_job, name, cfg).result()
        else:
            # Explicit opt-out (IBO_RFC_PROCESSES=0): old in-process path.
            from collectors.rfc_live import read_live, attach_snapshot_extras
            payload = read_live(name, cfg)
            payload = attach_snapshot_extras(payload, load_snapshot(name))
    except Exception as exc:
        payload = {"system": name, "connected": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    with _live_cache_lock:
        _live_cache[name] = payload
        _live_cache_at[name] = time_module.monotonic()
    # Performance RCA: every live poll is one observation for the trigger.
    # Cheap (a dict lookup and a counter); the expensive part only runs
    # when it decides to fire, and then in its own thread.
    try:
        _rca_observe(name, payload)
    except Exception as exc:   # noqa: BLE001 -- the wall must never stall on this
        log.debug(f"RCA trigger observe failed for {name}: {type(exc).__name__}: {exc}")


def _live_refresh_loop():
    from concurrent.futures import ThreadPoolExecutor, wait
    log.info(f"Live refresher started -- background RFC reads every {_LIVE_REFRESH_S}s "
             f"(pass ceiling {_LIVE_PASS_TIMEOUT_S}s); HTTP requests never call RFC directly.")
    while True:
        started = time_module.monotonic()
        try:
            systems = list(get_systems())
            if systems:
                # Cap concurrency at 3, not 8. The bound that matters here is
                # not CPU -- these threads are almost entirely blocked on RFC
                # network I/O -- it is the SAP side: five simultaneous cold
                # reads, each running a STAT table scan and a SQLM query, is
                # what pushed the first pass past its ceiling. Three at a time
                # keeps every system read within the window while still
                # finishing a five-system sweep in well under the cadence once
                # the perf/deep caches are warm. A pool per pass, not shared,
                # so a wedged RFC worker cannot be inherited by the next pass;
                # not closed with a context manager because __exit__ joins
                # every worker, which is the blocking this timeout avoids.
                workers = min(int(os.environ.get("IBO_LIVE_WORKERS", "3") or 3),
                              len(systems))
                pool = ThreadPoolExecutor(max_workers=workers,
                                          thread_name_prefix="live-rfc")
                futures = [pool.submit(_refresh_one_live, cfg) for cfg in systems]
                done, pending = wait(futures, timeout=_LIVE_PASS_TIMEOUT_S)
                if pending:
                    log.warning(
                        f"Live refresh pass hit the {_LIVE_PASS_TIMEOUT_S}s ceiling with "
                        f"{len(pending)} system(s) still reading. Their cached values "
                        f"are held and they are left to finish in the background.")
                pool.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 -- the loop must never die
            log.warning(f"Live refresh pass failed: {type(exc).__name__}: {exc}")

        # Sleep the REMAINDER of the interval, not a further full interval.
        # sleep(_LIVE_REFRESH_S) after a pass that itself took 20s produced an
        # 80-second effective cadence while the log claimed 60.
        elapsed = time_module.monotonic() - started
        time_module.sleep(max(1.0, _LIVE_REFRESH_S - elapsed))


def start_live_refresher():
    global _live_refresher_started
    if _live_refresher_started:
        return
    if _running_under_pytest():
        # See _running_under_pytest: this thread polls every configured
        # system over RFC, which a test run has no business doing.
        log.info("Live refresher not started: running under pytest.")
        _live_refresher_started = True
        return
    # ON-DEMAND BY DEFAULT. The timer-driven refresher is what read every
    # system every 60s -- including the unreachable ones -- whether anyone
    # was looking or not. It is now off unless explicitly enabled, so nothing
    # touches SAP until a person clicks "Read live" on the wall (which calls
    # /api/live/refresh). Set IBO_LIVE_AUTO_REFRESH=1 to restore the old
    # always-on polling.
    if str(os.environ.get("IBO_LIVE_AUTO_REFRESH", "0")).strip() not in ("1", "true", "yes"):
        log.info("Live refresher is ON-DEMAND -- systems are read only when "
                 "'Read live' is clicked (/api/live/refresh). "
                 "Set IBO_LIVE_AUTO_REFRESH=1 for the old timer-driven mode.")
        _live_refresher_started = True   # mark started so nothing else launches it
        return
    _live_refresher_started = True
    threading.Thread(target=_live_refresh_loop, daemon=True, name="live-refresher").start()


def _cached_live(system_name: str) -> dict | None:
    with _live_cache_lock:
        return _live_cache.get(system_name)


def _cached_live_age(system_name: str) -> float | None:
    with _live_cache_lock:
        at = _live_cache_at.get(system_name)
    return None if at is None else round(time_module.monotonic() - at, 1)


# Milestone 7.6: FastAPI lifespan lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preserve the existing scheduler startup behavior.
    start_scheduler()
    start_live_refresher()
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
    # Time every request and log anything slow. When "it's still slow" the
    # only thing that settles WHERE the time goes is this line: it names the
    # exact path and its server-side duration. If a request is slow here,
    # the server is the problem. If every request here is fast but the page
    # still lags, the time is in the browser or the network, not this code.
    t0 = time_module.perf_counter()
    response = await call_next(request)
    ms = (time_module.perf_counter() - t0) * 1000
    slow_ms = float(os.environ.get("IBO_SLOW_REQUEST_MS", "500") or 500)
    if ms >= slow_ms:
        log.warning(f"SLOW {request.method} {request.url.path} -> {ms:.0f} ms")
    response.headers["X-IBO-Server-Ms"] = f"{ms:.0f}"
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
                    "snooze_until": None, "manual_pause": False}


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
    return _page("index.html")


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
    return _get_live_impl(system_name)


_kicking: set[str] = set()
_kicking_lock = threading.Lock()


def _kick_live_refresh(system_name: str, cfg: dict) -> None:
    """
    Read one system in the background, off the request thread.

    Used when a page asks for a system the refresher has not reached yet.
    The guard stops a burst of page loads (or a fast-polling tab) firing ten
    concurrent reads of the same cold system -- the first kick reads it, the
    rest are no-ops until it lands in the cache.
    """
    with _kicking_lock:
        if system_name in _kicking:
            return
        _kicking.add(system_name)

    def _run():
        try:
            _refresh_one_live(cfg)
        except Exception as exc:  # noqa: BLE001 -- background best-effort
            log.warning(f"[{system_name}] kicked refresh failed: "
                        f"{type(exc).__name__}: {exc}")
        finally:
            with _kicking_lock:
                _kicking.discard(system_name)

    threading.Thread(target=_run, name=f"kick-{system_name}",
                     daemon=True).start()


def _get_live_impl(system_name: str):
    cfg = next((s for s in get_systems() if s.get("name") == system_name), None)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    payload = _cached_live(system_name)
    if payload is None:
        # Cold miss: the background refresher has not populated this system
        # yet. DO NOT do a synchronous RFC read here -- that is what made a
        # page load block for seconds, up to the full connect timeout on an
        # unreachable system like CEQ. Instead:
        #   1. kick the background refresher to read this system now, off the
        #      request thread, so the next poll finds it warm;
        #   2. answer this request immediately from the last saved snapshot
        #      (stale but real, clearly labelled), or a "warming" marker if
        #      there is not even a snapshot yet.
        # The page's loader animation covers the one poll it takes to warm.
        _kick_live_refresh(system_name, cfg)
        snap = load_snapshot(system_name)
        if snap is not None:
            from collectors.rfc_live import attach_snapshot_extras
            payload = attach_snapshot_extras({}, snap)
            payload["warming"] = True
            payload["from_snapshot"] = True
        else:
            payload = {"system": system_name, "warming": True,
                       "connected": False, "checks": [],
                       "note": "First read in progress -- reading live RFC…"}
        payload["cache_age_seconds"] = None
        return payload
    payload = dict(payload)
    payload["cache_age_seconds"] = _cached_live_age(system_name)
    return payload


@app.get("/api/live")
def get_live_all():
    """
    Every configured system, for the wall display.

    Reads the background refresher's cache only (see start_live_refresher
    near the top of this file) -- this endpoint never calls RFC itself, so
    it always returns in the time it takes to copy a few dicts, regardless
    of how slow or unreachable any SAP system currently is.
    """
    systems = list(get_systems())
    out = []
    for cfg in systems:
        name = cfg.get("name", "?")
        payload = _cached_live(name)
        if payload is None:
            # Warming-up stub. It MUST carry the same keys the wall's card()
            # renderer reads unguarded (dispatcher, icm), or one warming
            # system throws in card(), the whole .map(card) throws, and the
            # wall stays stuck on "Reading live RFC…" forever -- which is
            # exactly the hang this shape prevents. Kicks a background read so
            # the next poll finds it warm.
            _kick_live_refresh(name, cfg)
            payload = {
                "system": name, "sid": cfg.get("sap_system_id") or name,
                "client": cfg.get("client", ""),
                "connected": False, "warming_up": True,
                "error": None, "cooldown": 0,
                "cpu": None, "memory": None, "load_1m": None,
                "smon": {}, "checks": [], "instances": [],
                "work_processes": {},
                "dispatcher": {"label": "…", "status": "UNKNOWN", "source": ""},
                "icm": {"label": "…", "status": "UNKNOWN", "source": ""},
            }
        out.append(payload)

    live = sum(1 for s in out if s.get("connected"))
    return {
        "systems": out,
        "live_count": live,
        "total": len(out),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cache_age_seconds": max([a for a in (_cached_live_age(c.get("name", "?")) for c in systems) if a is not None], default=None),
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

def _asset_version() -> str:
    """
    A cache-buster derived from shell.js and shell.css mtimes.

    The pages hardcoded ?v=ds3 in forty places. Every patch to shell.js left
    that tag unchanged, so browsers -- Incognito included, once the session
    had fetched it -- kept serving the OLD shell.js. Fixes that were on disk
    never ran. This derives the tag from the files themselves, so changing
    either file changes the URL and forces a fresh fetch automatically.
    """
    stamp = 0
    for fn in ("shell.js", "shell.css"):
        try:
            stamp = max(stamp, int(os.stat(os.path.join(STATIC_DIR, fn)).st_mtime))
        except OSError:
            pass
    return str(stamp) if stamp else "0"


_page_cache: dict[str, tuple[float, str]] = {}


def _page(name: str):
    """
    Serve a page with its asset version stamped in.

    Rewrites any `?v=<tag>` on shell.js / shell.css to the live mtime-based
    version. The rewritten HTML is cached against the page's own mtime, so
    this costs a stat per request, not a read + regex.
    """
    from fastapi.responses import HTMLResponse
    path = os.path.join(STATIC_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"Page not built: {name}")
    mtime = os.stat(path).st_mtime
    ver = _asset_version()
    key = f"{name}@{ver}"
    hit = _page_cache.get(key)
    if hit and hit[0] == mtime:
        return HTMLResponse(hit[1], headers={"Cache-Control": "no-cache"})
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    html = re.sub(r"(shell\.(?:js|css))\?v=[A-Za-z0-9_.-]+", rf"\1?v={ver}", html)
    _page_cache[key] = (mtime, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/sky")
def sky_page():
    return _page("sky.html")


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


# ---------------------------------------------------------------------------
# Authentication for state-changing endpoints
# ---------------------------------------------------------------------------
#
# Until now the only thing preventing anyone on this host from deleting a
# monitored system or triggering a sweep was the 127.0.0.1 bind. That is not
# authentication -- any local process, any browser tab, any XSS on an
# unrelated page served from localhost could call these.
#
# Read endpoints stay open: they are what the dashboard polls, and adding a
# token to every poll buys little. The mutating ones are gated.
#
# Set IBO_API_TOKEN in .env. If it is unset the gate FAILS CLOSED and the
# mutating endpoints refuse -- an unset token must not silently mean "no
# security", which is how this kind of control quietly stops working.

def require_token(x_ibo_token: str = Header(default="")):
    expected = os.getenv("IBO_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="IBO_API_TOKEN is not set. State-changing endpoints are "
                   "disabled until it is configured in .env.")
    # compare_digest keeps the comparison constant-time so a token cannot be
    # recovered one character at a time by timing the response.
    if not secrets.compare_digest(x_ibo_token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-IBO-Token")
    return True


@app.get("/profiles-page")
def profiles_page():
    return _page("profiles.html")


def _enabled_profile_count() -> int:
    """How many profiles will actually run. Zero means a quiet scheduler."""
    try:
        from core.profiles import load_profiles
        return sum(1 for p in load_profiles() if p["schedule"].get("enabled"))
    except Exception:
        return 0


@app.post("/api/capabilities/reset", dependencies=[Depends(require_token)])
def api_reset_capabilities(system: str = ""):
    """
    Forget which tables and fields each system was found to support.

    Those answers are cached for the life of the process so a system missing
    a field costs one failed read rather than one every ten seconds. After a
    transport adds a table or a field, this makes the next poll probe again
    without restarting the server.
    """
    from collectors.rfc_live import reset_capabilities
    reset_capabilities(system or None)
    return {"reset": system or "all systems"}


@app.get("/api/delivery-accounts")
def api_delivery_accounts():
    """Named sender accounts. Secrets reported as configured, never returned."""
    from core.delivery_accounts import load_accounts
    return load_accounts()


@app.post("/api/delivery-accounts", dependencies=[Depends(require_token)])
def api_save_delivery_accounts(payload: dict):
    from core.delivery_accounts import save_accounts
    try:
        return {"saved": True,
                "accounts": save_accounts(payload.get("accounts") or {},
                                          payload.get("secrets"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.error(f"Could not save delivery accounts: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/delivery-settings")
def api_delivery_settings():
    """
    Sender configuration.

    Secrets are reported as configured or not -- never their value, not even
    masked. A masked password is still a length hint, and it still ends up in
    whatever screenshot gets pasted into a ticket.
    """
    from core.delivery_settings import read_settings
    return read_settings()


@app.post("/api/delivery-settings", dependencies=[Depends(require_token)])
def api_save_delivery_settings(payload: dict):
    """
    Update sender configuration in .env.

    Restricted to a fixed allow-list of keys: an endpoint that can set any
    environment variable could set IBO_API_TOKEN and lock everyone out, or
    repoint a system at another host.
    """
    from core.delivery_settings import save_settings
    try:
        return {"saved": True,
                "settings": save_settings(payload.get("plain"),
                                          payload.get("secrets"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.error(f"Could not save delivery settings: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/profiles/options")
def api_profile_options():
    """Everything the Profiles page needs to render its pickers."""
    from core.profiles import VALID_CHANNELS, available_tcodes
    return {
        "tcodes": available_tcodes(),
        "systems": [s.get("name") for s in get_systems() if s.get("name")],
        "channels": list(VALID_CHANNELS),
        "modes": ["minutes", "hours", "daily"],
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    }


class ProfilesPayload(BaseModel):
    profiles: list[dict]
    alerts: dict | None = None


@app.post("/api/profiles", dependencies=[Depends(require_token)])
def api_save_profiles(payload: ProfilesPayload):
    """
    Replace the profile set.

    Validation lives in core.profiles.save_profiles, not here: the YAML can
    also be edited by hand, and a rule enforced only through this endpoint is
    not enforced.
    """
    from core.profiles import save_profiles, status

    try:
        save_profiles(payload.profiles, payload.alerts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.error(f"Could not save profiles: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not save: {exc}")

    return {"saved": True, "profiles": status()}


@app.post("/api/profiles/alerts", dependencies=[Depends(require_token)])
def api_save_alert_routing(payload: dict):
    from core.profiles import alert_routing, save_alerts
    try:
        save_alerts(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"saved": True, "alerts": alert_routing()}


@app.get("/api/profiles")
def api_profiles():
    """Every monitoring profile with its schedule, T-codes and destinations."""
    from core.profiles import alert_routing, status
    return {"profiles": status(), "alerts": alert_routing()}


@app.post("/api/profiles/{profile_id}/run",
          dependencies=[Depends(require_token)])
def api_profile_run(profile_id: str):
    """
    Run one profile now, outside its schedule.

    Gated: a sweep drives SAP GUI on the monitoring host and takes minutes,
    so it is not something a stray page should be able to start.
    """
    from core.profiles import load_profiles, resolve_tcodes

    profile = next((p for p in load_profiles() if p["id"] == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"No profile {profile_id}")

    tcodes = resolve_tcodes(profile)
    if not tcodes:
        raise HTTPException(
            status_code=400,
            detail=(f"Profile {profile_id} resolves to no T-codes. Check that "
                    f"its tcodes exist in monitoring_tasks.yaml."))

    if _run_state["running"]:
        raise HTTPException(
            status_code=409,
            detail=f"A run is already in progress "
                   f"({_run_state.get('current_system') or 'all systems'}).")

    if not any(s.get("name") == profile["system"] for s in get_systems()):
        raise HTTPException(status_code=404,
                            detail=f"Unknown system: {profile['system']}")

    thread = threading.Thread(target=_run_one_system_background,
                              args=(profile["system"],), daemon=True)
    thread.start()

    # The sweep currently captures every configured T-code regardless of
    # profile; the profile decides what is DELIVERED. Restricting capture per
    # profile is a change inside the pipeline, and until it lands, saying so
    # here is better than implying a filter that is not applied.
    return {"started": True, "scope": profile["system"],
            "profile": profile_id,
            "delivers": profile["deliver"],
            "tcodes_selected": tcodes,
            "note": "All configured T-codes are captured; this profile's "
                    "selection controls delivery."}


@app.get("/api/auth-token")
def api_auth_token():
    """
    Hands the dashboard its own token.

    This is not a secret from anyone who can reach this port -- the app binds
    to 127.0.0.1, so anybody who can call this endpoint could already call the
    gated ones from a shell. The gate exists to stop a stray page, an
    extension, or a script from deleting a monitored system by accident, and
    to make every state change attributable to a caller that knows the token.

    Returns empty when unset so the UI can say "not configured" rather than
    failing with an opaque 401 on every button.
    """
    return {"token": os.getenv("IBO_API_TOKEN", "").strip(),
            "configured": bool(os.getenv("IBO_API_TOKEN", "").strip())}


@app.get("/incidents-page")
def incidents_page():
    return _page("incidents.html")


@app.get("/correlation-page")
def correlation_page():
    return _page("correlation.html")


@app.get("/rca-page")
def rca_page():
    return _page("rca.html")


def _catalogue_entry(rule_id: str | None) -> dict:
    """
    The deterministic half of an incident: which component is responsible, the
    SAP parameters that govern it, and the remediation steps.

    Attached to every incident so the detail view is more than an AI
    paragraph. The model writes one sentence; this is the part someone can
    act on, and it comes from config/correlation_rules.yaml, not a model.
    """
    if not rule_id:
        return {}
    try:
        from core.correlation import load_correlation_config
        raw = (load_correlation_config() or {}).get(rule_id) or {}
    except Exception:
        return {}
    return {
        "title": raw.get("title") or rule_id,
        "description": raw.get("description") or "",
        "confidence": raw.get("confidence"),
        "culprit": raw.get("culprit") or {},
        "parameters": raw.get("parameters") or [],
        "remediation": raw.get("remediation") or [],
        "rfc_evidence": raw.get("rfc_evidence") or [],
        "thresholds": raw.get("thresholds") or {},
    }


@app.get("/api/incidents")
def api_incidents(status: str = "", system: str = ""):
    """
    Correlated incidents across all configured systems.

    Empty is a real answer, not a failure: it means no correlation rule
    matched. The UI says so explicitly rather than showing a blank panel,
    because "no incidents" and "the screen is broken" look identical
    otherwise.
    """
    from core.incident_store import load_incidents

    wanted_status = status.strip().upper()
    rows, systems_read, errors = [], [], []

    for cfg in get_systems():
        name = cfg.get("name")
        if not name or (system and name != system):
            continue
        systems_read.append(name)
        try:
            for incident in load_incidents(name):
                data = incident.to_dict()
                current = str(data.get("status") or "").upper()
                if wanted_status and current != wanted_status:
                    continue
                data["duration_seconds"] = incident.duration_seconds
                data["catalogue"] = _catalogue_entry(data.get("rule_id"))
                rows.append(data)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    rows.sort(key=lambda r: str(r.get("last_seen") or ""), reverse=True)

    open_count = sum(1 for r in rows
                     if str(r.get("status") or "").upper() != "RESOLVED")
    with_ai = sum(1 for r in rows if r.get("ai_analysis"))

    return {
        "incidents": rows,
        "systems_read": systems_read,
        "counts": {"total": len(rows), "open": open_count,
                   "resolved": len(rows) - open_count, "with_ai": with_ai},
        "errors": errors,
    }


@app.get("/api/events")
def api_events(system: str = ""):
    """Raw events, the input correlation works from."""
    import json as _json
    import os as _os
    from utils.paths import BASE_DIR as _BASE

    events_dir = _os.path.join(_BASE, "dashboard", "events")
    rows, errors = [], []

    for cfg in get_systems():
        name = cfg.get("name")
        if not name or (system and name != system):
            continue
        path = _os.path.join(events_dir, f"{name}.json")
        if not _os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = _json.load(handle)
            items = payload.get("events") if isinstance(payload, dict) else payload
            for item in items or []:
                item = dict(item)
                item.setdefault("system", name)
                rows.append(item)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    rows.sort(key=lambda r: str(r.get("last_seen") or r.get("first_seen") or ""),
              reverse=True)
    return {"events": rows, "count": len(rows), "errors": errors}


@app.get("/api/correlation/rules")
def api_correlation_rules():
    """
    The deterministic rules, their thresholds, and how many incidents each
    has actually produced. A rule that has never fired is either wrongly
    tuned or covering something that does not happen -- both worth seeing.
    """
    from core.correlation import load_correlation_config
    from core.incident_store import load_incidents

    try:
        config = load_correlation_config()
    except Exception as exc:
        return {"rules": [], "error": f"{type(exc).__name__}: {exc}"}

    fired: dict[str, int] = {}
    for cfg in get_systems():
        name = cfg.get("name")
        if not name:
            continue
        try:
            for incident in load_incidents(name):
                fired[incident.rule_id] = fired.get(incident.rule_id, 0) + 1
        except Exception:
            continue

    rules = []
    for rule_id, raw in config.items():
        rules.append({
            "id": rule_id,
            "title": raw.get("title") or rule_id,
            "enabled": bool(raw.get("enabled", True)),
            "severity": raw.get("severity") or "",
            "description": raw.get("description") or "",
            "confidence": raw.get("confidence"),
            "thresholds": raw.get("thresholds") or {},
            "culprit": raw.get("culprit") or {},
            "remediation": raw.get("remediation") or [],
            "incidents_matched": fired.get(rule_id, 0),
        })

    rules.sort(key=lambda r: (-r["incidents_matched"], r["id"]))

    # A rule with no matcher loads, displays a threshold, and is then silently
    # skipped by the engine. That is worse than a missing rule: it looks like
    # coverage that does not exist.
    try:
        from core.correlation import unmatched_rules
        unmatched = unmatched_rules()
    except Exception:
        unmatched = []
    for rule in rules:
        rule["can_fire"] = rule["id"] not in unmatched

    return {"rules": rules, "total_incidents": sum(fired.values()),
            "unmatched": unmatched}


_refresh_run = {"in_progress": False, "started_at": None, "finished_at": None,
                "done": 0, "total": 0, "error": None}
_refresh_run_lock = threading.Lock()


@app.post("/api/live/refresh", dependencies=[Depends(require_token)])
def refresh_live_now():
    """
    Read every system once, now, in the background.

    This is the on-demand replacement for the timer. The wall's "Read live"
    button calls it. It returns immediately with a job marker; the wall then
    polls /api/live (which serves the cache being filled) and
    /api/live/refresh/status to know when the pass is done. Reads run
    concurrently, bounded, and each system lands in the cache as it finishes
    -- so cards fill in progressively rather than all at the end.
    """
    with _refresh_run_lock:
        if _refresh_run["in_progress"]:
            return {"started": False, "reason": "already_running", **_refresh_run}
        systems = list(get_systems())
        _refresh_run.update(in_progress=True,
                            started_at=datetime.now().strftime("%H:%M:%S"),
                            finished_at=None, done=0, total=len(systems),
                            error=None)

    def _run():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            workers = min(int(os.environ.get("IBO_LIVE_WORKERS", "3") or 3),
                          max(1, len(systems)))
            pool = ThreadPoolExecutor(max_workers=workers,
                                      thread_name_prefix="ondemand-rfc")
            futures = [pool.submit(_refresh_one_live, cfg) for cfg in systems]
            for _ in as_completed(futures):
                with _refresh_run_lock:
                    _refresh_run["done"] += 1
            pool.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 -- report, never crash the thread
            with _refresh_run_lock:
                _refresh_run["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with _refresh_run_lock:
                _refresh_run["in_progress"] = False
                _refresh_run["finished_at"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=_run, daemon=True, name="ondemand-refresh").start()
    with _refresh_run_lock:
        return {"started": True, **_refresh_run}


@app.get("/api/live/refresh/status")
def refresh_live_status():
    with _refresh_run_lock:
        return dict(_refresh_run)


@app.post("/api/rca/analyze/{system_name}", dependencies=[Depends(require_token)])
def api_rca_analyze(system_name: str):
    """
    On-demand RCA for one system, from a LIVE RFC read.

    Deliberately POST and deliberately not on a timer. Every call spends
    tokens, so it runs when a human asks and never on the scheduler -- four
    systems on a five-minute cycle would be roughly a thousand model calls a
    day to say "nothing is wrong" almost every time.

    The split is the same as everywhere else in this project: findings,
    severities and thresholds are computed here from the live readings, and
    the model is given those findings to write a narrative around. It cannot
    add a finding, change a severity or invent a remediation step.
    """
    from collectors.rfc_live import read_live
    from core.models import MonitoringResult, MetricResult, Status
    from evaluation.ai_analyzer import analyze, get_provider

    systems = {c.get("name"): c for c in get_systems()}
    cfg = systems.get(system_name)
    if not cfg:
        raise HTTPException(status_code=404,
                            detail=f"{system_name} is not in systems.yaml")

    # The live read runs in a worker process for the same reason the
    # refresher does: on this pyrfc build an RFC call holds the GIL, and a
    # read done here froze every other request for its whole duration.
    pool = _get_rfc_pool()
    if pool is not None:
        from collectors.live_job import read_live_job
        payload = pool.submit(read_live_job, system_name, cfg).result()
    else:
        from collectors.rfc_live import read_live
        payload = read_live(system_name, cfg, use_cache=False)

    if not payload.get("connected", False):
        # Refuse rather than analyse nothing. Asking a model to explain an
        # absence of data produces confident prose about a system it never
        # saw, which is worse than no answer.
        return {
            "system": system_name,
            "analysed": False,
            "reason": "not_connected",
            "detail": payload.get("error") or "System did not answer over RFC.",
            "findings": [],
        }

    # Findings are computed here, from the live readings, before the model is
    # called. Thresholds and the zero-handling rules live in core.live_metrics
    # so this endpoint, the health report and the alert preview cannot drift
    # apart -- a metric graded WARNING on one screen and NORMAL on another
    # destroys trust in both.
    from core.live_metrics import metrics_from_live

    result = MonitoringResult(system=system_name,
                              client=str(cfg.get("client") or ""))
    result.metrics = metrics_from_live(payload)
    result.overall_status = result.compute_overall_status()

    findings = [
        {"name": m.name, "value": m.display_value,
         "status": m.status.value, "detail": m.detail, "source": m.source}
        for m in result.metrics
        if m.status in (Status.CRITICAL, Status.WARNING, Status.UNKNOWN)
    ]

    # Dump breakdown as an explicit finding, so the panel names who and what
    # is dumping even when the model is unavailable. The count alone is in
    # findings above via the ST22 metric; this adds the attribution behind it
    # (top users, hosts, programs) as a structured finding the UI can render
    # without waiting on a narrative.
    dd = payload.get("dump_detail") or {}
    dump_count = dd.get("count")
    if dump_count is None:
        st22 = next((m for m in result.metrics if m.name == "sap.st22.dumps"), None)
        dump_count = int(st22.value) if st22 and st22.value is not None else 0
    if dump_count and dump_count > 0:
        top_users = dd.get("by_user") or []
        top_progs = dd.get("by_program") or []
        bits = []
        if top_users:
            bits.append("top users: " + ", ".join(
                f"{u.get('name') or u.get('user')} ({u.get('count')})"
                for u in top_users[:3]))
        if top_progs:
            bits.append("top programs: " + ", ".join(
                f"{p.get('program')} ({p.get('count')})" for p in top_progs[:3]))
        elif dd.get("note"):
            bits.append(dd["note"])
        findings.append({
            "name": "sap.st22.dump_breakdown",
            "value": f"{dump_count} dumps today",
            "status": "CRITICAL" if dump_count > 25 else
                      "WARNING" if dump_count > 5 else "NORMAL",
            "detail": "; ".join(bits) if bits else "no breakdown available",
            "source": "RFC · SNAP",
            "dump_detail": {
                "count": dump_count,
                "by_user": top_users[:6],
                "by_host": dd.get("by_host") or [],
                "by_program": top_progs[:6],
                "recent": dd.get("recent") or [],
                "program_source": dd.get("program_source"),
                "note": dd.get("note"),
            },
        })

    # ---- attribution: the named things behind the counters ---------------
    from core.attribution import build_attribution
    try:
        attribution = build_attribution(payload, result.metrics)
    except Exception as exc:  # noqa: BLE001 -- never blocks the analysis
        attribution = {"gaps": [f"attribution failed: {type(exc).__name__}: {exc}"]}

    # ---- the one model call ----------------------------------------------
    provider = get_provider()
    narrative, error = "", None
    analysis = None
    try:
        analysis = analyze(result, provider=provider, attribution=attribution)
        narrative = getattr(analysis, "likely_root_cause", "") or ""
    except Exception as exc:
        # A failed model call must not take the findings down with it. They
        # are the useful part and they were computed before the call.
        error = f"{type(exc).__name__}: {exc}"

    # Full structured analysis, not just the one-line root cause. The model
    # already produces severity, category, evidence, actions and limitations
    # via the RCA schema; the endpoint used to keep only likely_root_cause and
    # drop the rest. Deterministic severity/findings remain authoritative --
    # this is the model's explanation OVER them, clearly labelled.
    detail = None
    if analysis is not None:
        detail = {
            "root_cause": getattr(analysis, "likely_root_cause", "") or "",
            "root_cause_category": getattr(analysis, "root_cause_category", "") or "",
            "severity": getattr(analysis, "severity", "") or "",
            "finding_status": getattr(analysis, "finding_status", "") or "HYPOTHESIS",
            "confidence": getattr(analysis, "confidence", "") or "",
            "confidence_score": getattr(analysis, "confidence_score", None),
            "supporting_evidence": list(getattr(analysis, "evidence", []) or []),
            "contradicting_evidence": list(getattr(analysis, "contradicting_evidence", []) or []),
            "recommended_actions": list(getattr(analysis, "recommended_actions", []) or []),
            "limitations": list(getattr(analysis, "limitations", []) or []),
        }

    return {
        "system": system_name,
        "analysed": True,
        "overall_status": result.overall_status.value,
        "metric_count": len(result.metrics),
        "findings": findings,
        "attribution": attribution,
        "narrative": narrative,
        "analysis": detail,
        "narrative_error": error,
        "provider": (provider.status() if hasattr(provider, "status")
                     else {"provider": getattr(provider, "name", "?")}),
        "read_at": payload.get("read_at"),
    }


@app.get("/api/rca")
def api_rca(system: str = ""):
    """
    Incidents that carry an AI analysis, newest first.

    Only the narrative is model-written; severity, evidence and remediation
    come from the rules. The UI labels it so nobody acts on the paragraph as
    though it were a finding.
    """
    from core.incident_store import load_incidents

    rows = []
    for cfg in get_systems():
        name = cfg.get("name")
        if not name or (system and name != system):
            continue
        try:
            for incident in load_incidents(name):
                if not incident.ai_analysis:
                    continue
                data = incident.to_dict()
                data["duration_seconds"] = incident.duration_seconds
                data["catalogue"] = _catalogue_entry(data.get("rule_id"))
                rows.append(data)
        except Exception:
            continue

    rows.sort(key=lambda r: str(r.get("ai_analysis_at") or r.get("last_seen") or ""),
              reverse=True)

    provider_status = {}
    try:
        from evaluation.ai_analyzer import get_provider
        provider = get_provider()
        provider_status = (provider.status() if hasattr(provider, "status")
                           else {"provider": getattr(provider, "name", "?")})
    except Exception as exc:
        provider_status = {"error": f"{type(exc).__name__}: {exc}"}

    return {"analyses": rows, "count": len(rows), "provider": provider_status}


@app.get("/reports-page")
def reports_page():
    return _page("reports.html")


@app.get("/api/report/{system_name}")
def get_health_report(system_name: str, live: bool = False):
    """
    Findings, recommended solutions and a health summary for one system.

    Default is the last completed sweep, which carries the full metric set
    including GUI evidence. `?live=true` builds the report from a fresh RFC
    read instead -- everything the RFC path can see, with no sweep and no
    snapshot required.

    The live report is genuinely smaller, not just fresher: screenshots, OCR
    evidence and anything SSH-only are absent because RFC cannot produce them.
    Those metrics are reported UNKNOWN rather than omitted, so a thinner
    report never reads as a healthier one.
    """
    from core.status_snapshot import load_snapshot as _load
    from evaluation.health_report import build_report
    from core.models import MonitoringResult, MetricResult, Status
    from datetime import datetime as _dt

    if live:
        from core.live_metrics import build_live_result

        systems = {c.get("name"): c for c in get_systems()}
        cfg = systems.get(system_name)
        if not cfg:
            raise HTTPException(status_code=404,
                                detail=f"{system_name} is not in systems.yaml")

        result, payload = build_live_result(system_name, cfg)

        if not payload.get("connected", False):
            raise HTTPException(
                status_code=503,
                detail=(f"{system_name} did not answer over RFC. "
                        f"{payload.get('error') or ''}").strip())

        report = build_report(result)
        report["source"] = "live_rfc"
        report["read_at"] = payload.get("read_at")
        report["note"] = ("Built from a live RFC read. GUI screenshots, OCR "
                          "evidence and SSH-only metrics are not available on "
                          "this path and are reported UNKNOWN, not omitted.")
        return report

    snapshot = _load(system_name)
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"No monitoring snapshot for {system_name}. "
                   f"Run a sweep, or request ?live=true for an RFC-only report.")

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
    """
    Historical samples for ONE metric on ONE system.

    This previously called read_metric_history(metric_name) and discarded
    system_name entirely, so a PRD memory chart contained QAS readings as
    well. A mixed series is worse than no series: it looks authoritative.

    Falls back to the old Excel reader only when the JSONL store has nothing
    for this system yet, so existing charts keep working while history builds.
    """
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")

    from core.metric_history import read_series
    points = read_series(system_name, metric_name, days=days)
    if points:
        return points

    legacy = read_metric_history(metric_name, days=days)
    for point in legacy:
        point["source"] = "legacy Excel history -- not filtered by system"
    return legacy


@app.get("/api/trend/{system_name}/{metric_name}")
def get_trend(system_name: str, metric_name: str, days: int = 7):
    """Current reading against the same metric `days` ago."""
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")
    from core.metric_history import trend
    return trend(system_name, metric_name, days=days)


@app.get("/api/trends/{system_name}")
def get_trends(system_name: str, days: int = 7):
    """Trends for every metric with recorded history on this system."""
    import glob as _glob
    import json as _json
    import os as _os
    from core.metric_history import HISTORY_DIR, trend, _safe

    folder = _os.path.join(HISTORY_DIR, _safe(system_name))
    names: set[str] = set()
    for path in _glob.glob(_os.path.join(folder, "*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        names.add(_json.loads(line)["m"])
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue

    results = [trend(system_name, name, days=days) for name in sorted(names)]
    # Movers first: a metric that has not changed is not what anyone opened
    # this page to see.
    results.sort(key=lambda t: abs(t.get("change_percent") or 0), reverse=True)
    return {"system": system_name, "days": days, "trends": results}


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


@app.post("/api/systems", dependencies=[Depends(require_token)])
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
    except ValueError as exc:
        # Validation problems (duplicate name, bad field) are the caller's to
        # fix, and the message is ours, so it is safe to return.
        raise HTTPException(status_code=400, detail=f"Could not add system: {exc}")
    except Exception as exc:  # noqa: BLE001
        # Anything else is a server fault. Its message may carry a file path,
        # a YAML parser trace or a credential fragment -- log it here, where
        # it belongs, and give the client a fixed string. This used to return
        # str(exc) in the 400 body, which test_security_hardening exists to
        # catch.
        log.error("create_system failed for %r: %s: %s",
                  system.name, type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Unable to create system.")

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


@app.delete("/api/systems/{system_name}", dependencies=[Depends(require_token)])
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
        "current_profile": _run_state.get("profile_id"),
        "last_profile": _scheduler_state.get("last_profile"),
        # Scheduling is profile-driven. interval_minutes is kept only so the
        # older Scheduler screen still renders; nothing runs on it.
        "source": "profiles",
        "enabled_profiles": _enabled_profile_count(),
    }


@app.post("/api/scheduler/snooze", dependencies=[Depends(require_token)])
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


@app.post("/api/scheduler/{action}", dependencies=[Depends(require_token)])
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


@app.post("/api/schedules/{system_name}", dependencies=[Depends(require_token)])
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


@app.post("/api/scheduler-settings", dependencies=[Depends(require_token)])
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


@app.post("/api/run-stop", dependencies=[Depends(require_token)])
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


@app.post("/api/run-now", dependencies=[Depends(require_token)])
def run_now():
    if _run_state["running"]:
        raise HTTPException(status_code=409, detail="A run is already in progress.")
    thread = threading.Thread(target=_run_all_systems_background, daemon=True)
    thread.start()
    return {"started": True, "scope": "all"}


def _run_one_system_background(system_name: str, profile_id: str | None = None,
                               resume_scheduler_after: bool = False):
    """
    Runs the full pipeline for a single system, then clears run state.

    `profile_id` records which profile asked for this run, so the UI can say
    why a sweep is happening. A run that appears with no explanation is the
    thing this change was made to remove.

    `resume_scheduler_after` is False on purpose for a MANUAL run: stopping
    is the operator's decision, so a manual run leaves automatic monitoring
    paused rather than quietly turning it back on and chaining into a
    scheduled sweep. It clears only the transient manual_pause marker.
    """
    with _run_lock:
        if _run_state["running"]:
            return
        _run_state["running"] = True
        _run_state["error"] = None
        _run_state["cancel_requested"] = False
        _run_state["current_system"] = system_name
        _run_state["profile_id"] = profile_id
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
            _run_state["profile_id"] = None
        # Clear the transient manual-pause marker. The scheduler stays
        # disabled (a manual run does not re-arm it); the operator turns
        # automatic monitoring back on with Resume when they want it. This
        # is what stops a manual sweep from chaining into a scheduled one.
        _scheduler_state["manual_pause"] = False
        if resume_scheduler_after:
            _scheduler_state["enabled"] = True


@app.post("/api/run-now/{system_name}", dependencies=[Depends(require_token)])
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

    # A manual run must not silently hand off to a scheduled one. Before this,
    # the scheduler stayed enabled during a manual sweep, so the moment the
    # manual run freed _run_state the scheduler's next 20s pass could find a
    # profile due and launch it -- which is exactly how a manual CARFOUR run
    # was followed by an unrequested PRD sweep. Snooze automatic monitoring
    # for the duration; the background worker lifts the snooze when it ends.
    _scheduler_state["enabled"] = False
    _scheduler_state["manual_pause"] = True

    thread = threading.Thread(target=_run_one_system_background,
                              args=(system_name,),
                              kwargs={"resume_scheduler_after": False},
                              daemon=True)
    thread.start()
    return {"started": True, "scope": system_name,
            "note": "Automatic monitoring is paused during this manual run."}


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
        log.info("Scheduler: no run on startup.")
    else:
        log.info("Scheduler: MONITOR_ON_STARTUP=true -- due profiles will run shortly.")

    try:
        from core.profiles import load_profiles
        enabled = [p for p in load_profiles() if p["schedule"].get("enabled")]
        if enabled:
            log.info("Scheduler: profile-driven. " + "; ".join(
                f"{p['id']} ({p['system']} -> {','.join(p['deliver']) or 'no destination'})"
                for p in enabled))
        else:
            # Said explicitly. A quiet scheduler and a broken one look
            # identical in a log that says nothing.
            log.warning("Scheduler: no enabled profiles -- nothing will run "
                        "automatically. Add or enable one on the Profiles page.")
    except Exception as exc:
        log.error(f"Scheduler: could not read profiles: {exc}")

    while True:
        try:
            snooze = _scheduler_state.get("snooze_until")
            snoozed = bool(snooze and datetime.now() < snooze)
            if snooze and not snoozed:
                _scheduler_state["snooze_until"] = None
                log.info("Snooze expired -- automatic monitoring resumed.")

            if _scheduler_state["enabled"] and not snoozed and not _run_state["running"]:
                # PROFILES ARE THE ONLY SOURCE OF SCHEDULED RUNS.
                #
                # The previous behaviour ran every system on a global interval
                # plus a per-system schedule in schedules.json. That meant a
                # sweep could start at a time nobody had chosen -- and on a
                # production system a sweep drives SAP GUI for several minutes
                # and emails a report. Monitoring should happen when someone
                # asked for it, not because a default interval elapsed.
                #
                # Nothing runs now unless a profile says so. A landscape with
                # no enabled profile is quiet, and that is the correct
                # behaviour: silence because nothing was scheduled is honest,
                # where a surprise sweep is not.
                from core.profiles import (due_profiles, mark_run,
                                           status as profile_status)

                due = due_profiles()
                if due:
                    profile = due[0]
                    log.info(f"Scheduler: profile '{profile['id']}' is due "
                             f"({profile['system']}, "
                             f"{len(profile['resolved_tcodes'])} T-code(s), "
                             f"-> {profile['deliver'] or 'no destination'}).")
                    _scheduler_state["last_cycle_start"] = \
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _scheduler_state["last_profile"] = profile["id"]
                    # Marked before the run, not after: a sweep that crashes
                    # must not become due again immediately and loop.
                    mark_run(profile["id"])
                    _run_one_system_background(profile["system"],
                                               profile_id=profile["id"])
                    # AND re-mark at completion. A multi-minute sweep on a
                    # short interval could otherwise be due again the instant
                    # it finished (next_run = start + interval, already in the
                    # past), producing back-to-back scheduled sweeps nobody
                    # asked for. Stamping the FINISH time makes the interval
                    # count from when the sweep ended.
                    try:
                        mark_run(profile["id"])
                    except Exception:
                        pass
                    # One profile per pass, then re-evaluate. A long sweep
                    # would otherwise let several profiles pile up behind it.
                else:
                    upcoming = [p["next_run"] for p in profile_status()
                                if p.get("next_run")]
                    _scheduler_state["next_run_at"] = (
                        min(upcoming).replace("T", " ") if upcoming else None)
        except Exception as exc:
            # The scheduler must never die: a bad schedule entry would
            # otherwise stop all automatic monitoring until someone noticed.
            log.error(f"Scheduler pass failed: {type(exc).__name__}: {exc}")

        # Short sleep so a pause from the UI takes effect within seconds
        # rather than at the end of a multi-hour interval.
        time_module.sleep(20)


def start_scheduler():
    if _running_under_pytest():
        log.info("Scheduler not started: running under pytest.")
        return
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
    log.info("Scheduler started -- runs only what the Profiles page schedules.")


# reports/ holds GUI screenshots and Excel sheets containing production
# usernames, job names and lock owners. Mounting it as StaticFiles made every
# one of those browsable to anything that could reach this port. It is served
# through a route instead, with path containment enforced.

@app.get("/reports/{path:path}")
def serve_report_file(path: str):
    if not os.path.isdir(REPORTS_DIR):
        raise HTTPException(status_code=404, detail="No reports directory")

    # Resolve and confirm the result is still inside REPORTS_DIR. Without
    # this, "../../.env" resolves out of the tree and serves the credentials
    # file -- the exact secrets this project is already trying to contain.
    root = os.path.realpath(REPORTS_DIR)
    target = os.path.realpath(os.path.join(root, path))
    if not (target == root or target.startswith(root + os.sep)):
        raise HTTPException(status_code=403, detail="Path outside reports directory")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Not found")

    return FileResponse(target)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ===========================================================================
# Performance RCA -- threshold trigger and operator button
#
# Two entry points into one runner:
#   _rca_observe()      called once per live poll per system by
#                       _refresh_one_live(); fires when the trigger says so
#   POST /api/rca/{s}   the "Performance monitoring" button
#
# The runner owns _run_state for its duration, exactly like a sweep, so a
# scheduled sweep cannot start underneath it and the wall shows RUNNING.
# Pre-emption of an in-flight sweep is cooperative -- see core.rca_trigger.
# ===========================================================================

from core.rca_trigger import RcaTrigger, TriggerConfig, preempt_running_sweep   # noqa: E402

_rca_cfg: dict = {}
_rca_trigger: RcaTrigger | None = None
_rca_state = {"running": False, "system": None, "started_at": None, "last": {}}
_rca_lock = threading.Lock()


def _rca_load():
    global _rca_cfg, _rca_trigger
    from evaluation.rca_pipeline import load_rca_config, rca_enabled
    _rca_cfg = load_rca_config()
    tc = TriggerConfig.from_yaml(_rca_cfg)
    tc.enabled = tc.enabled and rca_enabled(_rca_cfg)
    _rca_trigger = RcaTrigger(tc)
    log.info(f"Performance RCA {'enabled' if tc.enabled else 'disabled'}: "
             f"{tc.metric} > {tc.threshold_ms:.0f} ms for {tc.sustain_polls} polls, "
             f"cooldown {tc.cooldown_minutes:.0f} min, preempt={tc.preempt_running_sweep}")


def _rca_observe(system_name: str, payload: dict) -> None:
    if _rca_trigger is None:
        _rca_load()
    d = _rca_trigger.observe(system_name, payload)
    if d.fire:
        _rca_start(system_name, d.reason, manual=False)


def _rca_start(system_name: str, reason: str, manual: bool) -> dict:
    """Launch the RCA in a thread. Returns immediately with what happened."""
    if _rca_trigger is None:
        _rca_load()
    with _rca_lock:
        if _rca_state["running"]:
            return {"started": False, "reason": f"RCA already running on {_rca_state['system']}"}
        _rca_state.update({"running": True, "system": system_name,
                           "started_at": datetime.now().isoformat(timespec="seconds")})
    if manual:
        _rca_trigger.mark_fired(system_name, reason)
        try:
            from core.rca_trigger import mark_fired_on_disk

            mark_fired_on_disk(system_name, reason)   # shared with sweep-triggered runs
        except Exception as exc:   # noqa: BLE001
            log.debug(f"RCA state not shared: {exc}")
    t = threading.Thread(target=_run_rca_background, args=(system_name, reason), daemon=True)
    t.start()
    return {"started": True, "system": system_name, "reason": reason}


def _run_rca_background(system_name: str, reason: str) -> None:
    from evaluation.rca_pipeline import run_rca
    outcome: dict = {"system": system_name, "reason": reason, "ok": False}
    try:
        cfg = next((c for c in get_systems() if c.get("name") == system_name), None)
        if cfg is None:
            outcome["error"] = f"Unknown system: {system_name}"
            return

        # ---- pre-empt whatever is holding SAP GUI -------------------------
        if _rca_trigger.cfg.preempt_running_sweep:
            ok, note = preempt_running_sweep(
                _run_state,
                request_cancel=lambda: _run_state.__setitem__("cancel_requested", True),
                is_running=lambda: bool(_run_state["running"]),
            )
            outcome["preempt"] = note
            if not ok:
                outcome["error"] = note
                log.error(f"RCA on {system_name} abandoned: {note}")
                return
        elif _run_state["running"]:
            outcome["error"] = f"a sweep is running on {_run_state.get('current_system')} and preempt is off"
            return

        # ---- own the run state so nothing else starts a sweep -------------
        with _run_lock:
            if _run_state["running"]:
                outcome["error"] = "sweep restarted before RCA could take the session"
                return
            _run_state.update({"running": True, "error": None, "cancel_requested": False,
                               "current_system": system_name, "profile_id": "rca"})
        try:
            outcome.update(run_rca(cfg, reason, cfg=_rca_cfg))
            outcome["ok"] = True
        finally:
            with _run_lock:
                _run_state.update({"running": False, "current_system": None, "profile_id": None})
    except Exception as exc:   # noqa: BLE001
        log.error(f"RCA run failed for {system_name}: {type(exc).__name__}: {exc}")
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        outcome["finished_at"] = datetime.now().isoformat(timespec="seconds")
        with _rca_lock:
            _rca_state.update({"running": False, "system": None, "last": outcome})


@app.post("/api/perf-rca/{system_name}", dependencies=[Depends(require_token)])
def rca_run_now(system_name: str):
    """The Performance monitoring button. Starts an RCA on one system now."""
    if not any(c.get("name") == system_name for c in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")
    if _rca_trigger is None:
        _rca_load()
    if not _rca_trigger.cfg.enabled:
        raise HTTPException(status_code=409, detail=(
            "Performance RCA is disabled. Set IBO_ENABLE_RCA=1 in .env and enabled: true "
            "in config/rca.yaml, then restart."))
    r = _rca_start(system_name, "manual: Performance monitoring button", manual=True)
    if not r.get("started"):
        raise HTTPException(status_code=409, detail=r.get("reason", "busy"))
    r["note"] = (f"Pre-empting the running sweep on {_run_state.get('current_system')}; "
                 f"the RCA starts once it yields the SAP GUI session."
                 if _run_state["running"] else "Starting now.")
    return r


@app.get("/api/perf-rca/{system_name}/status")
def rca_status(system_name: str):
    if _rca_trigger is None:
        _rca_load()
    with _rca_lock:
        st = dict(_rca_state)
    return {"trigger": _rca_trigger.status(system_name),
            "running": st["running"] and st["system"] == system_name,
            "running_system": st["system"], "started_at": st["started_at"],
            "last": st["last"] if (st["last"] or {}).get("system") == system_name else {},
            "sweep_running": bool(_run_state["running"]),
            "sweep_system": _run_state.get("current_system")}

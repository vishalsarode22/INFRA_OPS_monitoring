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
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

from core.status_snapshot import load_snapshot, list_snapshot_systems as _list_snapshot_systems_all
from dashboard.intelligence_api import router as intelligence_router
from core.config_loader import (
    get_systems, get_systems_safe, add_system, delete_system, get_monitoring_tasks,
    get_scheduler_interval_minutes, set_scheduler_interval_minutes,
)
from core.production_health import liveness, readiness
from reporting.history_reader import read_metric_history
from utils.logger import get_logger

log = get_logger(__name__, "dashboard")

# Real-time exception handling (core/errors.py): every unhandled error in any
# thread, every ERROR log record, is captured, grouped and shown on /errors.
# Not under pytest -- the suite raises errors on purpose.
if "pytest" not in sys.modules:
    try:
        from core import errors as _errors
        _errors.install()
    except Exception as _exc:  # noqa: BLE001
        log.warning(f"error capture not installed: {_exc}")


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


def _get_rfc_pool():
    """
    Truthy when live RFC reads run in worker PROCESSES (the default), None
    when IBO_RFC_PROCESSES=0 asks for the old in-process reads.

    Why processes: the middleware timing proved the whole interpreter freezes
    during RFC activity (a static JPEG took 30s, /api/live took 62s, both
    spanning RFC bursts): pyrfc holds the GIL for the duration of its calls
    on this build. Threads share that GIL; processes do not.

    Each SAP system now has its OWN worker process (core/rfc_workers.py).
    With one shared pool, a single crashing or hanging read broke every other
    system's read (BrokenProcessPool / "cancelled twice by worker restarts").
    """
    from core import rfc_workers
    return rfc_workers if rfc_workers.enabled() else None


def _rfc_call_timeout() -> float:
    try:
        return max(5.0, float(os.environ.get("IBO_RFC_CALL_TIMEOUT", "120") or 120))
    except ValueError:
        return 120.0


from core.rfc_workers import RfcCallTimeout, RfcWorkerDied, RfcBusy  # noqa: E402


_rfc_crashes: dict = {}


def _run_in_rfc_pool(fn, *args):
    """
    fn(*args) in the worker process of the system named by args[0].

    A whole live read is many RFC calls, so it may take two call limits
    (2 x IBO_RFC_CALL_TIMEOUT) from when it starts; queueing for a free slot
    does not count. A crash is retried once on a fresh process inside
    rfc_workers; a second crash is recorded on the Errors page with the
    system name and the process's exit code.
    """
    from core import rfc_workers
    if not rfc_workers.enabled():
        return fn(*args)
    key = str(args[0]) if args and isinstance(args[0], str) else getattr(fn, "__name__", "rfc")
    try:
        return rfc_workers.run(key, fn, tuple(args), timeout=2 * _rfc_call_timeout())
    except RfcWorkerDied as exc:
        # A crash inside SAP's RFC library (exit 0xC0000005 / 0xC0000374) is
        # retried on a fresh process and the next read normally works. It is
        # an error worth acting on (update the SAP NetWeaver RFC SDK) only when
        # it keeps happening: 3 times in 6 hours for the same system.
        now = time_module.time()
        hits = [t for t in _rfc_crashes.get(key, []) if now - t < 6 * 3600] + [now]
        _rfc_crashes[key] = hits
        if len(hits) >= 3:
            try:
                from core import errors as _errors
                _errors.capture(exc, "loop", context=f"{exc} -- {len(hits)} times in 6 h for {key}. "
                                "Update the SAP NetWeaver RFC SDK on this machine (the crash is inside it).")
            except Exception:
                pass
        else:
            log.warning(f"{exc} (crash {len(hits)} of 3 in 6 h before it is reported as an error)")
        raise


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
            payload = _run_in_rfc_pool(read_live_job, name, cfg)
        else:
            # Explicit opt-out (IBO_RFC_PROCESSES=0): old in-process path.
            from collectors.rfc_live import read_live, attach_snapshot_extras
            payload = read_live(name, cfg)
            payload = attach_snapshot_extras(payload, load_snapshot(name))
    except RfcBusy:
        # The previous read of this system is still running (slow or hung):
        # keep showing the last reading, do not queue another read behind it.
        return
    except Exception as exc:
        payload = {"system": name, "connected": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict):
        # A read that returned nothing usable used to be cached as None, which
        # crashed this function and made every wall poll start another read.
        payload = {"system": name, "connected": False,
                   "error": f"the RFC read returned no data ({type(payload).__name__})"}
    with _live_cache_lock:
        # The wall gets a copy with long row lists capped; alerts and history
        # below still see the full reading.
        _live_cache[name] = _trim_payload(payload)
        _live_cache_at[name] = time_module.monotonic()
    # Every live read is a history sample (throttled to one per metric per
    # 5 min inside the store) -- the raw material for learned baselines and
    # capacity forecasts (core/learned_baseline.py, core/capacity.py).
    if payload.get("connected"):
        try:
            from core.live_metrics import metrics_from_live
            from core.metric_history import append_metrics
            append_metrics(name, metrics_from_live(payload))
        except Exception as exc:  # noqa: BLE001
            log.debug(f"history sample for {name} skipped: {exc}")
    # Performance RCA: every live poll is one observation for the trigger.
    # Cheap (a dict lookup and a counter); the expensive part only runs
    # when it decides to fire, and then in its own thread.
    try:
        _rca_observe(name, payload)
    except Exception as exc:   # noqa: BLE001 -- the wall must never stall on this
        log.debug(f"RCA trigger observe failed for {name}: {type(exc).__name__}: {exc}")
    # Alerts chosen per value on the Thresholds page (core/alert_engine.py).
    # Sending runs in its own thread; this only decides.
    if payload.get("connected"):
        try:
            from core import alert_engine
            graded = _grade_live(payload)
            alert_engine.observe(cfg, graded.get("graded_items") or [], source="live")
        except Exception as exc:   # noqa: BLE001
            log.warning(f"alert check failed for {name}: {type(exc).__name__}: {exc}")


def _trim_payload(payload: dict) -> dict:
    """
    Cap the row lists a live reading carries to IBO_LIVE_MAX_ROWS (500) each.
    Counts are computed by the collector before this; the lists are only for
    display. One system with 50,000 dump rows made every wall poll 13 MB.
    """
    try:
        cap = max(50, int(os.getenv("IBO_LIVE_MAX_ROWS", "500") or 500))
    except ValueError:
        cap = 500

    def trim(d: dict, depth: int) -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, list) and len(v) > cap:
                out[k] = v[:cap]
                out[f"{k}_truncated_from"] = len(v)
            elif isinstance(v, dict) and depth < 2:
                out[k] = trim(v, depth + 1)
            else:
                out[k] = v
        return out
    try:
        return trim(payload, 0)
    except Exception:  # noqa: BLE001 -- never lose a reading over this
        return payload


def _standby_active(cfg: dict) -> bool:
    """False for a standby system whose usual monitor is up (core/standby.py)."""
    try:
        from core import standby
        return standby.active(cfg)
    except Exception:
        return True


def _live_poll_due(cfg: dict) -> bool:
    """True when this system's own polling interval has passed."""
    try:
        every = int(cfg.get("live_refresh_seconds") or 0)
    except (TypeError, ValueError):
        every = 0
    if every <= _LIVE_REFRESH_S:
        return True
    with _live_cache_lock:
        last = _live_cache_at.get(cfg.get("name"))
    # A small allowance so a 300 s system is not skipped because the loop
    # woke a second early.
    return last is None or time_module.monotonic() - last >= every - 5


def _live_refresh_loop():
    from concurrent.futures import ThreadPoolExecutor, wait
    log.info(f"Live refresher started -- background RFC reads every {_LIVE_REFRESH_S}s "
             f"(pass ceiling {_LIVE_PASS_TIMEOUT_S}s); HTTP requests never call RFC directly.")
    while True:
        started = time_module.monotonic()
        try:
            # Round 37: a system can be polled less often than the global
            # IBO_LIVE_REFRESH_SECONDS with `live_refresh_seconds:` in its
            # systems.yaml entry (e.g. 300 for production). Skipped systems
            # keep their last reading. The Refresh button is not affected.
            systems = [c for c in get_systems() if _live_poll_due(c) and _standby_active(c)]
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
                # Reads not started yet are dropped (cancel_futures): they come
                # round again next pass. Before, an overrun pass left its whole
                # queue behind and the backlog grew every pass.
                pool.shutdown(wait=False, cancel_futures=True)
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
    from core import supervisor
    supervisor.start("live-refresher", _live_refresh_loop)


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
    # Watch the other services (jobs, collector) once a minute and, in a
    # single-process deployment (IBO_ROLE=all), keep our own heartbeat too.
    if not _running_under_pytest():
        def _watch_loop():
            from core import store
            while True:
                try:
                    if process_role() == "all":
                        store.beat("web", "single-process")
                        # No jobs service in single-process mode: re-probe
                        # failing systems here (5-min DOWN reminder, recovery).
                        from core.reachability import recheck_known_failures
                        recheck_known_failures()
                        # Maintenance-window ends, peer heartbeats, ticket
                        # sync, capacity forecasts (core/ops_tick.py).
                        from core import ops_tick
                        ops_tick.run()
                    _web_watchdog()
                except Exception as exc:
                    log.debug(f"watch loop: {exc}")
                time_module.sleep(60)
        from core import supervisor
        supervisor.start("service-watch", _watch_loop)
        try:
            from core.auth import bootstrap_admin
            bootstrap_admin()
        except Exception as exc:
            log.warning(f"auth bootstrap failed: {exc}")
    try:
        yield
    finally:
        # The scheduler thread remains daemonized, matching the old behavior.
        # Stop the per-system RFC worker processes with the dashboard.
        try:
            from core import rfc_workers
            rfc_workers.shutdown()
        except Exception:
            pass


app = FastAPI(
    title="InfraBeatOps Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

# Per-IP rate limit on every /api route (see dashboard/rate_limit.py for the
# settings). Registered BEFORE the security-headers middleware below, which
# makes it the inner layer: a 429 still gets the security headers and still
# shows up in the slow-request timing. Skipped under pytest so the existing
# contract tests, which make hundreds of calls from one TestClient, are not
# throttled; tests/test_rate_limit.py tests the limiter on its own.
from dashboard.rate_limit import RateLimitMiddleware
# "pytest" in sys.modules, not _running_under_pytest(): this line runs at
# import time, and test modules import the app during collection, before
# PYTEST_CURRENT_TEST is set.
import sys as _sys
if "pytest" not in _sys.modules and not _running_under_pytest():
    app.add_middleware(RateLimitMiddleware)

# ---------------------------------------------------------------------------
# Authentication, roles and customer scope (core/auth.py)
#
# Order matters: this middleware is registered BEFORE the security-headers
# one below, so it runs INSIDE it (Starlette wraps in reverse order) and
# every response, including a 401 or a login redirect, still gets the
# headers.
# ---------------------------------------------------------------------------

# Explicit allow-list of routes served without a principal. Everything else
# needs one: a session (users configured), a loopback client (no users /
# IBO_AUTH=0), or the API token as `Authorization: Bearer` (automation).
# /access/* are the e-mailed one-click links; each carries its own signed,
# single-use token (core/action_links.py).
_PUBLIC_PATHS = ("/healthz", "/readyz", "/login", "/logout", "/static/", "/access/action",
                 "/access/ticket/", "/auth/callback", "/favicon.ico",
                 # account pages: sign-up, e-mail confirmation, forgot / reset password
                 "/signup", "/verify", "/forgot", "/reset")

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# State-changing requests need operator by default. These need admin:
# systems, thresholds, settings, delivery accounts, .env-backed secrets,
# schedules/profiles (configuration), users.
_ADMIN_WRITE_PREFIXES = (
    "/api/systems", "/api/delivery-accounts", "/api/delivery-settings", "/api/profiles",
    "/api/tcode-sets", "/api/schedules", "/api/scheduler-settings", "/api/users",
    "/api/settings", "/api/ops/thresholds", "/api/ops/systems/validate",
    "/api/ops/systems/save", "/api/ops/config/", "/api/ops/alert-recipients",
    "/api/ops/alert-teams", "/api/ops/actions/approvals", "/api/ops/peers/",
    "/api/ops/health/test-mail", "/api/ops/alerts/test", "/api/ops/digest/send",
    "/api/ops/escalation/test-sms", "/api/capabilities/reset",
    # Clearing an RFC logon block without changing the password spends one more
    # failed SAP logon; repeated, that locks the SAP user. Admin only.
    "/api/logon-blocks/",
)
# ...except these, which are operator actions under an admin prefix.
_OPERATOR_WRITE_PATTERNS = (re.compile(r"^/api/profiles/[^/]+/run$"),)
# Any signed-in principal (viewer included) may POST these.
_VIEWER_WRITE_PATHS = ("/api/errors/client", "/api/access/requests", "/api/me/password")
# ...and these per-item ones (the handler checks it is the caller's own request).
_VIEWER_WRITE_PATTERNS = (re.compile(r"^/api/access/requests/\d+/withdraw$"),)
# All a signed-in person with no project yet may reach.
_NO_PROJECT_PATHS = ("/team", "/api/team", "/api/access/", "/api/me", "/api/errors/client")
# Unsafe methods that carry their own protection (signed link / no session yet).
_CSRF_EXEMPT = ("/login", "/logout", "/access/action", "/access/ticket/",
                "/signup", "/forgot", "/reset", "/verify")

_LOOPBACK_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient")


def _is_loopback_request(request) -> bool:
    """
    A client on this machine talking to us directly. A request relayed by a
    reverse proxy on the same host arrives from 127.0.0.1 too, but carries
    X-Forwarded-For / Forwarded -- that is a NETWORK client, not a local one.
    ("testclient" is Starlette's TestClient; no TCP peer can have that name.)
    """
    host = (request.client.host if request.client else "") or ""
    if host not in _LOOPBACK_ADDRS:
        return False
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded") \
            or request.headers.get("x-real-ip"):
        return False
    return True


def _trust_proxy() -> bool:
    return str(os.getenv("IBO_TRUST_PROXY", "0")).strip().lower() in ("1", "true", "yes", "on")


def _client_address(request) -> str:
    """
    The real client for login lockout. Behind the company reverse proxy every
    request comes from 127.0.0.1, so 20 wrong logins by anyone locked EVERYONE
    out. With IBO_TRUST_PROXY=1 the address the proxy appended last to
    X-Forwarded-For is used (the left-most entries are whatever the client
    sent and can be forged).
    """
    peer = (request.client.host if request.client else "") or ""
    if _trust_proxy() and peer in _LOOPBACK_ADDRS:
        xff = request.headers.get("x-forwarded-for") or ""
        last = xff.split(",")[-1].strip() if xff else ""
        if last:
            return last
        real = (request.headers.get("x-real-ip") or "").strip()
        if real:
            return real
    return peer


def _request_is_https(request) -> bool:
    if str(os.getenv("IBO_COOKIE_SECURE", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    if request.url.scheme == "https":
        return True
    if _trust_proxy():
        return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
    return False


_MAX_BODY = 2 * 1024 * 1024       # JSON writes; uploads are not JSON and are not read here


def _required_role(path: str, method: str) -> str:
    if method in _SAFE_METHODS:
        return "viewer"
    if path in _VIEWER_WRITE_PATHS or any(p.match(path) for p in _VIEWER_WRITE_PATTERNS):
        return "viewer"
    if any(p.match(path) for p in _OPERATOR_WRITE_PATTERNS):
        return "operator"
    if any(path == p or path.startswith(p) for p in _ADMIN_WRITE_PREFIXES):
        return "admin"
    return "operator"


def _same_origin(request) -> bool:
    """Origin (or Referer) must name this host. Absent both: not a browser form/fetch."""
    from urllib.parse import urlsplit
    host = (request.headers.get("host") or "").lower()
    origin = request.headers.get("origin")
    # Browsers also say where a request came from in Sec-Fetch-Site; it is
    # set by the browser itself and cannot be changed by a page.
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site in ("cross-site", "same-site"):
        return False
    if origin is not None:
        if origin.strip().lower() == "null":
            # A page served with no-referrer (an older dashboard tab) sends
            # Origin: null even to itself; Sec-Fetch-Site still tells.
            return site in ("same-origin", "none")
        return urlsplit(origin).netloc.lower() == host
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc.lower() == host
    if site == "same-origin":
        return True
    # No Origin, Referer or Sec-Fetch-Site at all: not a browser form or fetch
    # (scripts). The sign-in form is refused then (an old browser could be made
    # to log a victim into an attacker's account); API calls with a Bearer
    # token do not come through here.
    return request.url.path != "/login"


def _bearer_token(request) -> str | None:
    authz = request.headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        return authz[7:].strip()
    legacy = request.headers.get("x-ibo-token")   # legacy alias for existing scripts
    return legacy.strip() if legacy else None


_NO_USERS_HELP = ("No dashboard users are configured, so this dashboard only serves "
                  "the machine it runs on (http://127.0.0.1). An administrator must "
                  "create a login on the dashboard host: "
                  "python -m dashboard.manage_users add <name> --role admin")


def list_snapshot_systems() -> list[str]:
    """Snapshot names, limited to the request's customer scope."""
    from core.auth import current_user
    names = _list_snapshot_systems_all()
    if current_user.get() is None or current_user.get().get("customers") is None:
        return names
    allowed = {s.get("name") for s in get_systems()}
    allowed |= {_safe_system_name(n) for n in allowed}
    return [n for n in names if n in allowed]


def _safe_system_name(value) -> str:
    try:
        from reporting.system_paths import safe_system_name
        return safe_system_name(value)
    except Exception:
        return str(value)


def _scope_violation(request, principal: dict) -> bool:
    """
    True when the URL names a system outside the user's customer scope.
    Generic on purpose: path segments and the system / system_name query
    parameters are compared against every configured system name, so no
    endpoint has to be edited to be scoped.
    """
    if principal is None or (principal.get("customers") is None and principal.get("systems") is None):
        return False
    from core.config_loader import get_all_systems

    def norm(v) -> str:
        # The form the snapshot / history files use (letters, digits, - and _),
        # case-folded: "CEQ.", "C.EQ", "ceq" and "CEQ%20" all name CEQ
        # (security review 28.10.2026: they used to slip past this check).
        return "".join(c for c in str(v or "") if c.isalnum() or c in "-_").casefold()

    allowed_cfg = get_systems()
    allowed = {norm(s.get("name")) for s in allowed_cfg} | {norm(_safe_system_name(s.get("name"))) for s in allowed_cfg}
    everything = set()
    for s in get_all_systems():
        everything.add(norm(s.get("name")))
        everything.add(norm(_safe_system_name(s.get("name"))))
    everything.discard("")
    from urllib.parse import unquote
    candidates = [unquote(seg) for seg in request.url.path.split("/") if seg]
    candidates += [v for k, v in request.query_params.multi_items() if k in ("system", "system_name", "name")]
    return any(norm(c) in everything and norm(c) not in allowed for c in candidates)


@app.middleware("http")
async def _auth_middleware(request, call_next):
    """
    Authentication AND authorization for every route, in one place.

    1. Who:  Bearer API token (automation) | session cookie (users exist) |
             loopback client as local admin (no users, or IBO_AUTH=0).
    2. May they: public allow-list, else role by method/path
             (viewer reads; operator acts; admin configures).
    3. CSRF: a state-changing request from a browser principal must carry
             X-CSRF-Token (from /api/me) and come from this origin.
    """
    from core import auth
    from urllib.parse import quote
    path = request.url.path
    method = request.method.upper()
    public = any(path == p or path.startswith(p) for p in _PUBLIC_PATHS)
    is_api = path.startswith("/api/") or path.startswith("/reports/") or path.startswith("/report/")

    principal = None
    session_token = None
    bearer = _bearer_token(request)
    if bearer is not None:
        expected = os.getenv("IBO_API_TOKEN", "").strip()
        if not (expected and bearer and secrets.compare_digest(bearer, expected)):
            return JSONResponse(status_code=401, content={"detail": "Invalid API token."})
        principal = auth.api_token_principal()
    elif auth.login_required():
        session_token = request.cookies.get(auth.COOKIE)
        try:
            principal = auth.principal_for_token(session_token)
        except Exception as exc:
            log.warning(f"auth lookup failed: {type(exc).__name__}")
            principal = None
    else:
        # No users configured, or IBO_AUTH=0: the owner's local use only.
        if _is_loopback_request(request):
            principal = auth.local_admin_principal()
        elif not public:
            if is_api:
                return JSONResponse(status_code=403, content={"detail": _NO_USERS_HELP})
            import html as _h
            return HTMLResponse(status_code=403, content=(
                "<!doctype html><meta charset='utf-8'><title>InfraBeatOps</title>"
                f"<body style='font-family:system-ui;padding:2rem'><h1>Access denied</h1>"
                f"<p>{_h.escape(_NO_USERS_HELP)}</p></body>"))

    if principal is None and not public:
        if is_api:
            return JSONResponse(status_code=401, content={"detail": "login required"})
        return RedirectResponse(url=f"/login?next={quote(path, safe='/')}", status_code=303)

    if (principal is not None and not public and principal.get("role") != "admin"
            and principal.get("customers") is not None and not principal.get("customers")
            and not any(path == p or path.startswith(p) for p in _NO_PROJECT_PATHS)):
        # Signed in but no project yet (a fresh sign-up): only Team & access,
        # where they ask for one. Nothing else -- not even pages that are not
        # filtered by project (errors, health, logs).
        if is_api or method not in _SAFE_METHODS:
            return JSONResponse(status_code=403, content={
                "detail": "You have no project yet. Ask for access on the Team & access page."})
        return RedirectResponse(url="/team", status_code=303)

    if principal is not None and not public:
        # Who may do what, per project and system (core/access.py, core/projects.py).
        from core import access as _access
        _body = b""
        if method not in _SAFE_METHODS:
            try:
                if int(request.headers.get("content-length") or 0) > _MAX_BODY:
                    return JSONResponse(status_code=413, content={"detail": "Request too large."})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Bad Content-Length."})
            _ctype = (request.headers.get("content-type") or "").lower()
            # Every body FastAPI would parse as JSON (application/json, ...+json,
            # no content type) is read here too, so the check sees what the
            # handler will act on.
            if not _ctype.startswith("multipart/"):
                try:
                    _body = await request.body()
                except Exception:  # noqa: BLE001
                    _body = b""
                if len(_body) > _MAX_BODY:
                    return JSONResponse(status_code=413, content={"detail": "Request too large."})
        try:
            _ok, _why = _access.decide(principal, path, method, dict(request.query_params), _body)
        except Exception as exc:  # noqa: BLE001 -- a broken rule never opens the door
            log.error(f"access check failed for {method} {path}: {type(exc).__name__}: {exc}")
            _ok, _why = principal.get("role") == "admin", "access check failed"
        if not _ok:
            try:
                from core import projects as _pr
                if method not in _SAFE_METHODS:
                    _pr.audit(principal.get("username"), "denied", f"{method} {path}", _why)
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(status_code=404 if _why == _access.NOT_FOUND else 403,
                                content={"detail": _why})
        _cat = _access.category(path, method)
        if _cat not in ("read", "self") and principal.get("role") != "admin":
            principal = dict(principal, access_ok=_cat)

    if (method not in _SAFE_METHODS and principal is not None
            and principal.get("via") in ("session", "local")
            and not any(path == p or path.startswith(p) for p in _CSRF_EXEMPT)):
        presented = request.headers.get("x-csrf-token")
        if not (_same_origin(request) and auth.csrf_ok(principal, session_token, presented)):
            if principal.get("via") == "local" and not presented:
                return JSONResponse(status_code=401, content={
                    "detail": "Missing CSRF token. Browser pages send it automatically; "
                              "scripts use 'Authorization: Bearer <IBO_API_TOKEN>'."})
            return JSONResponse(status_code=403, content={"detail": "CSRF check failed. Reload the page."})

    request.state.session_token = session_token
    token_ctx = auth.current_user.set(principal)
    try:
        if principal is not None and _scope_violation(request, principal):
            return JSONResponse(status_code=404, content={"detail": "Unknown system"})
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            # Answer here: Starlette's own 500 path re-raised after sending,
            # which reset the keep-alive connection and broke the client's
            # NEXT request too (black-box test 28.10.2026).
            return await _unhandled_exception(request, exc)
    finally:
        auth.current_user.reset(token_ctx)


_LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>InfraBeatOps - sign in</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/static/brand/favicon.png">
<style>
@font-face{font-family:'IBOPS Inter';src:url(/static/fonts/inter-400.woff) format('woff');font-weight:400}
@font-face{font-family:'IBOPS Inter';src:url(/static/fonts/inter-600.woff) format('woff');font-weight:600}
@font-face{font-family:'IBOPS Display';src:url(/static/fonts/interdisplay-100.woff) format('woff');font-weight:100}
@font-face{font-family:'IBOPS Pixel';src:url(/static/fonts/unifont.woff) format('woff')}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:'IBOPS Inter','Segoe UI',system-ui,sans-serif;color:#EAF3FF;overflow-x:hidden;
  background:radial-gradient(90% 60% at 50% 30%,#0C2550 0%,#061533 45%,#030914 100%);
  display:flex;align-items:center;justify-content:center;position:relative}
body::before{content:"";position:fixed;left:-50%;right:-50%;bottom:-6%;height:40%;pointer-events:none;
  background:linear-gradient(rgba(102,173,248,.38) 1px,transparent 1px) 0 0/100% 46px,
             linear-gradient(90deg,rgba(102,173,248,.38) 1px,transparent 1px) 0 0/46px 100%;
  transform:perspective(420px) rotateX(64deg);transform-origin:50% 0;
  -webkit-mask-image:linear-gradient(to bottom,transparent,#000 30%,#000 70%,transparent);
          mask-image:linear-gradient(to bottom,transparent,#000 30%,#000 70%,transparent)}
body::after{content:"";position:fixed;left:0;right:0;top:66%;height:1px;pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(156,203,251,.85),transparent);box-shadow:0 0 24px 4px rgba(47,140,243,.4)}
.wrap{position:relative;z-index:1;width:min(92vw,380px);transform:translateY(-6vh)}
.mark{display:flex;justify-content:center;margin-bottom:22px}
.mark span{width:64px;height:64px;border-radius:50%;display:grid;place-items:center;position:relative;
  background:rgba(255,255,255,.95);box-shadow:0 0 0 8px rgba(47,140,243,.10),0 0 40px rgba(47,140,243,.45)}
.mark img{width:46px;height:46px;object-fit:contain}
.word{text-align:center;font-family:'IBOPS Display','Segoe UI',sans-serif;font-weight:100;font-size:26px;
  letter-spacing:.42em;text-indent:.42em;color:#F2F8FF;text-shadow:0 0 16px rgba(102,173,248,.55)}
.tag{text-align:center;font-family:'IBOPS Pixel',Consolas,monospace;font-size:16px;color:#8FB8E8;margin:8px 0 26px}
form{position:relative;padding:26px 26px 22px;border-radius:14px;
  background:rgba(10,30,62,.48);border:1px solid rgba(156,203,251,.22);
  backdrop-filter:blur(14px) saturate(1.3);-webkit-backdrop-filter:blur(14px) saturate(1.3);
  box-shadow:0 30px 80px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.08)}
/* HUD corners: the panel is framed like a heads-up display */
form::before,form::after,.c1,.c2{content:"";position:absolute;width:14px;height:14px;border:solid #9CCBFB;opacity:.9}
form::before{left:-1px;top:-1px;border-width:2px 0 0 2px;border-top-left-radius:14px}
form::after{right:-1px;bottom:-1px;border-width:0 2px 2px 0;border-bottom-right-radius:14px}
.c1{right:-1px;top:-1px;border-width:2px 2px 0 0;border-top-right-radius:14px}
.c2{left:-1px;bottom:-1px;border-width:0 0 2px 2px;border-bottom-left-radius:14px}
label{display:block;font-size:12px;font-weight:600;color:#A9C6E8;margin:0 0 6px}
input{width:100%;padding:10px 2px;margin-bottom:18px;border:0;border-bottom:1px solid rgba(156,203,251,.35);
  background:transparent;color:#F2F8FF;font:inherit;font-size:15px;outline:none;transition:border-color .2s,box-shadow .2s}
input:focus{border-bottom-color:#9CCBFB;box-shadow:0 1px 0 0 #66ADF8,0 8px 18px -12px #66ADF8}
input:-webkit-autofill{-webkit-text-fill-color:#F2F8FF;-webkit-box-shadow:0 0 0 40px #0b2246 inset;caret-color:#fff}
button{width:100%;margin-top:4px;padding:11px;border:1px solid rgba(156,203,251,.55);border-radius:9px;
  background:linear-gradient(180deg,#2F8CF3,#1E70D6);color:#fff;font:inherit;font-weight:600;font-size:14px;cursor:pointer;
  box-shadow:0 0 0 1px rgba(30,112,214,.4),0 10px 30px -8px rgba(47,140,243,.8)}
button:hover{background:linear-gradient(180deg,#4A9CF5,#2478DE)}
button:focus-visible{outline:2px solid #fff;outline-offset:2px}
a{color:#CFE4FF}
.err{margin-top:14px;padding:9px 11px;border-radius:8px;font-size:13px;color:#FFD5D2;
  background:rgba(230,51,41,.16);border:1px solid rgba(244,75,75,.45)}
.foot{text-align:center;margin-top:16px;font-size:12px;color:#7FA3CC}
.links{display:flex;justify-content:space-between;gap:12px;margin-top:16px;font-size:12.5px}
.links a{color:#9CCBFB;text-decoration:none}.links a:hover{text-decoration:underline}
.ok{margin-top:4px;padding:10px 12px;border-radius:8px;font-size:13.5px;line-height:1.5;color:#D8F5E6;
  background:rgba(59,229,138,.12);border:1px solid rgba(59,229,138,.40)}
.hint{font-size:11.5px;color:#7FA3CC;margin:-12px 0 16px}
h1.t{font-family:'IBOPS Display','Segoe UI',sans-serif;font-weight:100;font-size:20px;letter-spacing:.06em;
  color:#F2F8FF;margin:0 0 18px}
p.lead{font-size:13px;color:#A9C6E8;margin:-8px 0 18px;line-height:1.5}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head>
<body><div class="wrap">
<div class="mark"><span><img src="/static/brand/ibops-mark-64.png" alt=""></span></div>
<div class="word" aria-label="InfraBeatOps">INFRABEATOPS</div>
<div class="tag">SAP Basis monitoring</div>
<form method="post" action="/login"><i class="c1"></i><i class="c2"></i>
<input type="hidden" name="next" value="{{NEXT}}">
<label for="u">E-mail or username</label><input id="u" name="username" autocomplete="username" autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button>{{SSO}}{{ERROR}}
<div class="links"><a href="/forgot">Forgot password?</a><a href="/signup">Create an account</a></div></form>
<div class="foot">Infrabeat Technologies &middot; internal use</div>
</div></body></html>"""


def _render_login(next_path: str = "/", error: str = "") -> str:
    """
    Fill the login page. Plain placeholder replacement, NOT %-formatting:
    the inline CSS contains "100%;" which %-formatting reads as a format
    code (ValueError: unsupported format character ';'). The next path is
    HTML-escaped and limited to local paths so it cannot inject markup or
    redirect off-site.
    """
    import html as _html
    safe_next = next_path if (next_path or "").startswith("/") and not next_path.startswith("//") else "/"
    sso = ""
    try:
        from core import sso as _sso
        if _sso.configured():
            from urllib.parse import quote as _q
            sso = (f'<a href="/login/sso?next={_html.escape(_q(safe_next), quote=True)}" style="display:block;'
                   f'margin-top:10px;text-align:center;padding:.6rem;border-radius:6px;border:1px solid #334155;'
                   f'color:#e2e8f0;text-decoration:none;font-weight:600">Sign in with Microsoft</a>')
    except Exception:
        sso = ""
    return (_LOGIN_HTML.replace("{{NEXT}}", _html.escape(safe_next, quote=True))
                       .replace("{{SSO}}", sso).replace("{{ERROR}}", error))


def _set_session_cookie(resp, request, token: str) -> None:
    from core import auth
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict",
                    secure=_request_is_https(request),
                    max_age=auth.SESSION_MAX_HOURS * 3600, path="/")


def _safe_next(nxt: str) -> str:
    nxt = str(nxt or "/")
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        return "/"
    # Browsers drop tab/CR/LF from URLs: "/\t/evil.com" becomes "//evil.com".
    if any(ord(c) < 32 or ord(c) == 127 for c in nxt):
        return "/"
    from urllib.parse import urlsplit
    squeezed = "".join(nxt.split())
    parts = urlsplit(squeezed)
    if parts.scheme or parts.netloc or squeezed.startswith("//"):
        return "/"
    return nxt


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/"):
    from core import auth
    if not auth.login_required():
        # No users (or IBO_AUTH=0): nothing to log in to. Loopback clients are
        # the local admin; the middleware answers everyone else with 403.
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_render_login(next))


@app.post("/login")
async def login_submit(request: Request):
    from core import auth
    if not _same_origin(request):
        return HTMLResponse(_render_login("/", "<div class='err'>Cross-site sign-in refused.</div>"),
                            status_code=403)
    form = await request.form()
    username = str(form.get("username", ""))[:128]
    password = str(form.get("password", ""))[:1024]
    nxt = _safe_next(form.get("next", "/"))
    remote = _client_address(request)
    try:
        # The password check is deliberately slow (PBKDF2, ~65 ms of CPU). Run
        # on the event loop it froze every other request while it ran: 20
        # people signing in at once held /healthz up for 1.3 s.
        from starlette.concurrency import run_in_threadpool
        token = await run_in_threadpool(auth.login, username, password, remote)
    except auth.LoginLocked:
        return HTMLResponse(_render_login(nxt, "<div class='err'>Too many failed attempts. "
                                               "Try again in 15 minutes.</div>"), status_code=429)
    except auth.EmailNotVerified:
        return HTMLResponse(_render_login(nxt, "<div class='err'>Confirm your e-mail first: open the link "
                                               "in the mail we sent you. No mail? Use <a href='/forgot'>"
                                               "Forgot password</a>; that link confirms it too.</div>"),
                            status_code=403)
    if token is None:
        return HTMLResponse(_render_login(nxt, "<div class='err'>Wrong username or password.</div>"),
                            status_code=401)
    resp = RedirectResponse(url=nxt, status_code=303)
    _set_session_cookie(resp, request, token)
    return resp


@app.get("/login/sso")
def login_sso(next: str = "/"):
    from core import sso
    try:
        url, cookie = sso.start(next)
    except ValueError as exc:
        return HTMLResponse(_render_login(next, f"<div class='err'>{exc}</div>"), status_code=400)
    resp = RedirectResponse(url=url, status_code=303)
    resp.set_cookie(sso.COOKIE, cookie, httponly=True, samesite="lax", max_age=600, path="/",
                    secure=_render_secure_flag())
    return resp


def _render_secure_flag() -> bool:
    from core.action_links import public_url
    return public_url().lower().startswith("https://")


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = "", error: str = "",
                  error_description: str = ""):
    import html as _h
    from core import auth, sso
    if error:
        return HTMLResponse(_render_login("/", f"<div class='err'>Microsoft: {_h.escape(error_description or error)[:200]}</div>"),
                            status_code=400)
    remote = request.client.host if request.client else ""
    try:
        token, user, nxt = sso.finish(code, state, request.cookies.get(sso.COOKIE, ""), remote)
    except ValueError as exc:
        return HTMLResponse(_render_login("/", f"<div class='err'>{_h.escape(str(exc))}</div>"), status_code=403)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"sso callback failed: {type(exc).__name__}: {exc}")
        return HTMLResponse(_render_login("/", "<div class='err'>Microsoft sign-in failed -- see the Errors page.</div>"),
                            status_code=502)
    # The callback is a cross-site navigation (from Microsoft). A SameSite=
    # Strict cookie set here would not be sent on an immediate 303, so hand
    # the browser a same-site page that moves on by itself.
    target = _h.escape(_safe_next(nxt), quote=True)
    resp = HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='0;url={target}'>"
        f"<title>Signed in</title><a href='{target}'>Continue</a>")
    _set_session_cookie(resp, request, token)
    resp.delete_cookie(sso.COOKIE, path="/")
    return resp


@app.get("/logout")
@app.post("/logout")
def logout_route(request: Request):
    from core import auth
    tok = request.cookies.get(auth.COOKIE)
    if tok:
        try:
            auth.logout(tok)
        except Exception:
            pass
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    """
    Who the page is talking as, and the CSRF token its state-changing calls
    must send as X-CSRF-Token. Same-origin only (no CORS), so another site
    cannot read the token.
    """
    from core import auth
    user = auth.current_user.get()
    if user is None:
        return {"authenticated": False, "auth_enabled": auth.login_required()}
    local = user.get("via") == "local"
    out = {"authenticated": True, "username": user["username"], "role": user["role"],
           "role_label": auth.ROLE_LABELS.get(user["role"], user["role"]),
           "customers": "*" if user["customers"] is None else sorted(user["customers"]),
           "can_manage_access": bool(auth.grantable_roles(user)),
           "can_write": auth.may_write(), "is_admin": auth.is_admin(),
           "auth_enabled": auth.login_required(),
           "csrf_token": auth.csrf_token(user, getattr(request.state, "session_token", None)),
           "local_mode": local}
    if user["customers"] is not None and not user["customers"] and user.get("role") != "admin":
        out["banner"] = ("You have no project yet, so there is nothing to show. "
                         "Ask for access on the Team & access page; your manager approves it.")
        out["banner_href"] = "/team"
    if local:
        out["banner"] = ("No users configured: this dashboard is reachable from this machine "
                         "only, as admin. Create logins with "
                         "'python -m dashboard.manage_users add <name> --role admin'.")
    return out


@app.get("/api/users")
def api_users():
    from core import auth
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="admin only")
    return {"users": auth.list_users()}


# ---------------------------------------------------------------------------
# Accounts: sign-up, e-mail confirmation, forgot / reset password (public
# pages, same look as the sign-in page) and the Team & access API.
# Rules and roles: core/accounts.py.
# ---------------------------------------------------------------------------

def _account_page(inner: str, status_code: int = 200):
    """The sign-in page's frame with another form (or message) inside."""
    head, rest = _LOGIN_HTML.split('<form method="post" action="/login">', 1)
    tail = rest.split("</form>", 1)[1]
    return HTMLResponse(head + inner + tail, status_code=status_code)


def _h(text) -> str:
    import html as _hh
    return _hh.escape(str(text or ""), quote=True)


def _account_form(action: str, title: str, lead: str, fields: str, button: str,
                  message: str = "", links: str = "") -> str:
    return (f'<form method="post" action="{action}"><i class="c1"></i><i class="c2"></i>'
            f'<h1 class="t">{_h(title)}</h1>' + (f'<p class="lead">{lead}</p>' if lead else "")
            + fields + f'<button type="submit">{_h(button)}</button>{message}'
            + (links or '<div class="links"><a href="/login">Back to sign in</a></div>') + "</form>")


def _account_note(title: str, html_body: str, ok: bool = True, links: str = "") -> str:
    cls = "ok" if ok else "err"
    return (f'<form onsubmit="return false"><i class="c1"></i><i class="c2"></i><h1 class="t">{_h(title)}</h1>'
            f'<div class="{cls}">{html_body}</div>'
            + (links or '<div class="links"><a href="/login">Sign in</a></div>') + "</form>")


def _signup_form(message: str = "", name: str = "", email: str = "") -> str:
    from core import accounts, auth
    doms = ", ".join("@" + d for d in accounts.signup_domains())
    return _account_form(
        "/signup", "Create an account",
        f"For {_h(doms)} addresses. After you confirm your e-mail, ask for access to your projects; "
        f"your manager approves it.",
        f'<label for="n">Full name</label><input id="n" name="full_name" value="{_h(name)}" autocomplete="name" autofocus>'
        f'<label for="e">Work e-mail</label><input id="e" name="email" type="email" value="{_h(email)}" autocomplete="email" required>'
        f'<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="new-password" required>'
        f'<div class="hint">At least {auth.MIN_PASSWORD_LENGTH} characters.</div>'
        f'<label for="p2">Repeat password</label><input id="p2" name="password2" type="password" autocomplete="new-password" required>',
        "Create account", message,
        '<div class="links"><a href="/login">I have an account</a><a href="/forgot">Forgot password?</a></div>')


def _accounts_open() -> bool:
    from core import auth
    return auth.login_required()


@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    return _account_page(_signup_form())


@app.post("/signup")
async def signup_submit(request: Request):
    from core import accounts, auth
    from starlette.concurrency import run_in_threadpool
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    if not _same_origin(request):
        return _account_page(_signup_form("<div class='err'>Cross-site request refused.</div>"), 403)
    form = await request.form()
    name = str(form.get("full_name", ""))[:80]
    email = str(form.get("email", ""))[:120]
    pw, pw2 = str(form.get("password", ""))[:1024], str(form.get("password2", ""))[:1024]
    if pw != pw2:
        return _account_page(_signup_form("<div class='err'>The two passwords differ.</div>", name, email), 400)
    try:
        res = await run_in_threadpool(accounts.signup, email, name, pw, _client_address(request))
    except (accounts.AccountError, ValueError) as exc:
        return _account_page(_signup_form(f"<div class='err'>{_h(exc)}</div>", name, email), 400)
    if not res.get("mailed"):
        return _account_page(_account_note(
            "Mail not sent",
            "The confirmation mail could not be sent (mail is off or failing on this server). "
            "Ask an administrator to check the mail settings, or to confirm your account.", ok=False))
    return _account_page(_account_note(
        "Check your inbox",
        f"We sent a confirmation link to <b>{_h(email.strip().lower())}</b>. Open it to finish, then sign in "
        f"and ask for access to your projects."))


def _verify_form(token: str, who: str, message: str = "") -> str:
    return _account_form(
        "/verify", "Confirm your e-mail", f"For <b>{_h(who)}</b>. Enter the password you chose when you "
        f"created the account.",
        f'<input type="hidden" name="token" value="{_h(token)}">'
        f'<label for="p">Password</label><input id="p" name="password" type="password" '
        f'autocomplete="current-password" autofocus required>',
        "Confirm", message)


@app.get("/verify", response_class=HTMLResponse)
def verify_page(token: str = ""):
    """Shows a form only: a mail scanner opening the link confirms nothing."""
    from core import accounts
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    who = accounts.peek_token(token[:200], "verify") if token else None
    if not who:
        return _account_page(_account_note(
            "Link expired", "This confirmation link has expired or was already used. Sign in to see if your "
            "account is ready, or use <a href='/forgot'>Forgot password</a> to get a new link.", ok=False), 400)
    return _account_page(_verify_form(token, who))


@app.post("/verify")
async def verify_submit(request: Request):
    from core import accounts
    from starlette.concurrency import run_in_threadpool
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    token = str(form.get("token", ""))[:200]
    who = accounts.peek_token(token, "verify") or ""
    if not _same_origin(request):
        return _account_page(_verify_form(token, who, "<div class='err'>Cross-site request refused.</div>"), 403)
    try:
        user = await run_in_threadpool(accounts.verify, token, str(form.get("password", ""))[:1024])
    except accounts.AccountError as exc:
        return _account_page(_verify_form(token, who, f"<div class='err'>{_h(exc)}</div>"), 400)
    if not user:
        return _account_page(_account_note(
            "Link expired", "This confirmation link has expired or was already used. "
            "<a href='/forgot'>Get a new link</a>.", ok=False), 400)
    return _account_page(_account_note(
        "E-mail confirmed", f"<b>{_h(user)}</b> is ready. Sign in, then ask for access to your projects on the "
        f"Team &amp; access page.", links='<div class="links"><a href="/login?next=/team">Sign in</a></div>'))


def _forgot_form(message: str = "") -> str:
    return _account_form(
        "/forgot", "Forgot password", "Enter your e-mail. If it belongs to an account, we mail a link to "
        "choose a new password.",
        '<label for="e">E-mail or username</label><input id="e" name="email" autocomplete="username" autofocus required>',
        "Send link", message)


@app.get("/forgot", response_class=HTMLResponse)
def forgot_page():
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    return _account_page(_forgot_form())


@app.post("/forgot")
async def forgot_submit(request: Request):
    from core import accounts
    from starlette.concurrency import run_in_threadpool
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    if not _same_origin(request):
        return _account_page(_forgot_form("<div class='err'>Cross-site request refused.</div>"), 403)
    form = await request.form()
    await run_in_threadpool(accounts.forgot, str(form.get("email", ""))[:120], _client_address(request))
    # The same answer whether or not the account exists.
    return _account_page(_account_note(
        "Check your inbox", "If that is an account's e-mail, a link to choose a new password is on its way. "
        "It works once, for 30 minutes. Nothing arrived? Ask your manager to send you a new link."))


def _reset_form(token: str, message: str = "", who: str = "") -> str:
    from core import auth
    return _account_form(
        "/reset", "Choose a password", f"For <b>{_h(who)}</b>." if who else "",
        f'<input type="hidden" name="token" value="{_h(token)}">'
        f'<label for="p">New password</label><input id="p" name="password" type="password" autocomplete="new-password" autofocus required>'
        f'<div class="hint">At least {auth.MIN_PASSWORD_LENGTH} characters.</div>'
        f'<label for="p2">Repeat password</label><input id="p2" name="password2" type="password" autocomplete="new-password" required>',
        "Save password", message)


@app.get("/reset", response_class=HTMLResponse)
def reset_page(token: str = ""):
    from core import accounts
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    who = accounts.peek_token(token[:200], "reset") if token else None
    if not who:
        return _account_page(_account_note(
            "Link expired", "This link has expired or was already used. "
            "<a href='/forgot'>Ask for a new one</a>.", ok=False), 400)
    return _account_page(_reset_form(token, who=who))


@app.post("/reset")
async def reset_submit(request: Request):
    from core import accounts
    from starlette.concurrency import run_in_threadpool
    if not _accounts_open():
        return RedirectResponse(url="/", status_code=303)
    form = await request.form()
    token = str(form.get("token", ""))[:200]
    if not _same_origin(request):
        return _account_page(_reset_form(token, "<div class='err'>Cross-site request refused.</div>"), 403)
    pw, pw2 = str(form.get("password", ""))[:1024], str(form.get("password2", ""))[:1024]
    who = accounts.peek_token(token, "reset")
    if pw != pw2:
        return _account_page(_reset_form(token, "<div class='err'>The two passwords differ.</div>", who or ""), 400)
    try:
        user = await run_in_threadpool(accounts.reset_password, token, pw)
    except (accounts.AccountError, ValueError) as exc:
        return _account_page(_reset_form(token, f"<div class='err'>{_h(exc)}</div>", who or ""), 400)
    return _account_page(_account_note(
        "Password saved", f"Sign in as <b>{_h(user)}</b> with your new password. Any other open sessions "
        f"of this account were signed out."))


# ---- Team & access API -----------------------------------------------------

def _account_call(fn, *a, **kw):
    from core import accounts
    try:
        return fn(*a, **kw)
    except accounts.AccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/team")
def team_page():
    return _page("team.html")


@app.get("/api/team")
def api_team():
    """Everything the Team & access page shows, cut to what the caller may see."""
    from core import accounts, auth
    me = auth.current_user.get() or {}
    accounts.ensure_schema()
    grant = auth.grantable_roles(me)
    every = accounts.all_projects()
    mine = me.get("customers")
    managed = [p for p in every if accounts.manages_project(me, p)]
    row = accounts._user(me.get("username", ""))
    return {
        "me": {"username": me.get("username"), "role": me.get("role"),
               "role_label": auth.ROLE_LABELS.get(me.get("role"), me.get("role")),
               "projects": "*" if mine is None else sorted(mine),
               "email": accounts._mail_of(row) if row is not None else "",
               "full_name": row["full_name"] if row is not None else "",
               "has_account": row is not None},
        "projects": every,
        "managed_projects": managed,
        "grantable_roles": [{"id": r, "label": auth.ROLE_LABELS[r]} for r in grant],
        "requestable_roles": [{"id": r, "label": auth.ROLE_LABELS[r]} for r in auth.MANAGER_GRANTS
                              if r != "client"],
        "roles": [{"id": r, "label": auth.ROLE_LABELS[r], "level": auth.ROLE_LEVEL[r]}
                  for r in auth.ROLES if r != "operator"],
        "users": accounts.visible_users(me) if grant or me.get("role") in ("team_lead", "operator") else [],
        "requests": accounts.list_requests(me),
        "signup_domains": accounts.signup_domains(),
    }


class _AccessRequest(BaseModel):
    projects: list[str] = []
    role_wanted: str = ""
    note: str = ""


@app.post("/api/access/requests")
def api_access_request(body: _AccessRequest):
    from core import accounts, auth
    made = _account_call(accounts.request_access, auth.current_user.get(), body.projects,
                         body.role_wanted, body.note)
    return {"ok": True, "created": made}


@app.post("/api/access/requests/{request_id}/withdraw")
def api_access_withdraw(request_id: int):
    from core import accounts, auth
    _account_call(accounts.withdraw, auth.current_user.get(), request_id)
    return {"ok": True}


class _Decision(BaseModel):
    approve: bool
    role: str = ""
    note: str = ""


@app.post("/api/access/requests/{request_id}/decide")
def api_access_decide(request_id: int, body: _Decision):
    from core import accounts, auth
    return _account_call(accounts.decide, auth.current_user.get(), request_id, body.approve,
                         body.role, body.note)


class _Invite(BaseModel):
    email: str
    full_name: str = ""
    role: str
    projects: list[str] = []


@app.post("/api/team/users")
def api_team_invite(body: _Invite):
    from core import accounts, auth
    return _account_call(accounts.invite, auth.current_user.get(), body.email, body.full_name,
                         body.role, body.projects)


class _UserChange(BaseModel):
    role: str | None = None
    add_projects: list[str] = []
    remove_projects: list[str] = []
    all_projects: bool | None = None
    disabled: bool | None = None
    full_name: str | None = None


@app.post("/api/team/users/{username}")
def api_team_update(username: str, body: _UserChange):
    from core import accounts, auth
    return _account_call(accounts.update_user, auth.current_user.get(), username, role=body.role,
                         add=body.add_projects, remove=body.remove_projects,
                         all_projects_flag=body.all_projects, disabled=body.disabled,
                         full_name=body.full_name)


@app.post("/api/team/users/{username}/reset")
def api_team_reset(username: str):
    from core import accounts, auth
    return _account_call(accounts.send_reset_for, auth.current_user.get(), username)


class _PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/me/password")
def api_me_password(body: _PasswordChange):
    from core import accounts, auth
    me = auth.current_user.get() or {}
    if me.get("via") != "session":
        raise HTTPException(status_code=400, detail="Only a signed-in user can change a password.")
    _account_call(accounts.change_password, me["username"], body.current, body.new)
    return {"ok": True, "detail": "Password changed. Sign in again."}


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
    # same-origin, not no-referrer: with no-referrer the browser sends
    # "Origin: null" on the dashboard's own sign-in form, and the cross-site
    # check refused every login. Other sites still get no referrer.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    return response


# What the pages actually load: their own /static files, Chart.js from
# cdnjs (index.html), Google Fonts (shell.js loadFonts), the brand video.
# 'unsafe-inline' stays for script/style because every page still carries
# inline <script> blocks and onclick= handlers; the policy still blocks
# loading or sending anything to another origin (connect/img/form/frame),
# plugins and <base> hijacking. Untrusted text is escaped at render time
# (IB.esc) -- that, not the CSP, is the primary XSS defence.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; media-src 'self' blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self' https://login.microsoftonline.com; frame-ancestors 'none'"
)


def allowed_hosts() -> list[str]:
    """
    Host headers this app answers (TrustedHostMiddleware). Blocks DNS
    rebinding against the loopback-admin mode and host-header tricks.
    IBO_ALLOWED_HOSTS=dashboard.corp.local,10.1.2.3 adds names; the host of
    IBO_PUBLIC_URL is added automatically.
    """
    raw = os.getenv("IBO_ALLOWED_HOSTS", "localhost,127.0.0.1")
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    # This machine itself always: the supervisor's health check and a browser
    # on the VM use 127.0.0.1 / localhost even when the company name is added.
    for local in ("localhost", "127.0.0.1"):
        if local not in hosts:
            hosts.append(local)
    public = os.getenv("IBO_PUBLIC_URL", "").strip()
    if public:
        from urllib.parse import urlsplit
        name = urlsplit(public).hostname
        if name and name not in hosts:
            hosts.append(name)
    if "pytest" in sys.modules:
        hosts.append("testserver")       # Starlette TestClient's Host header
    return hosts or ["localhost", "127.0.0.1"]


from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

app.include_router(intelligence_router)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """
    Any exception escaping an endpoint: captured (core/errors.py) and answered
    with a 500 that carries an error id the user can quote -- never the
    exception text, which can hold paths or connection details.
    """
    err_id = None
    try:
        from core import errors as _errors
        err_id = _errors.capture(exc, "web", context=f"{request.method} {request.url.path}")
    except Exception:
        pass
    # warning, not error: the exception itself was captured just above, and an
    # ERROR line here would be captured a second time as a separate "log" error.
    log.warning(f"unhandled error on {request.method} {request.url.path}: {type(exc).__name__} (id {err_id})")
    return JSONResponse(status_code=500, content={
        "detail": "Internal error. It has been recorded on the Errors page"
                  + (f" as {err_id}" if err_id else "") + ".", "error_id": err_id})
# Operations: maintenance, snooze/ack, timeline, on-call, tickets, knowledge
# base, runbooks, one-click actions, health page, config editor.
from dashboard.ops_api import router as ops_router  # noqa: E402
app.include_router(ops_router)
# Projects & passwords (core/projects.py, core/vault.py).
from dashboard.access_api import router as access_router  # noqa: E402
app.include_router(access_router)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_history.json")

SCHEDULE_INTERVAL_MINUTES = get_scheduler_interval_minutes(default=120)

_run_state = {"running": False, "error": None, "current_system": None,
              "cancel_requested": False, "stopped_after": None,
              # set only by the Stop button (cancel_requested is also used by
              # an RCA to pre-empt a sweep, which must still send its sheet)
              "stop_requested": False,
              # Runs that were requested while another was in progress. A
              # dropped run used to vanish without a trace; this is how the
              # UI, run history and log all learn that coverage was missed.
              "skipped": [], "skipped_total": 0}


def _record_skipped_run(requested: str, profile_id=None, trigger: str | None = None,
                        planned_at=None) -> None:
    """Call with _run_lock held."""
    entry = {"requested": requested, "profile_id": profile_id,
             "blocked_by": _run_state.get("current_system"),
             "at": datetime.now().isoformat(timespec="seconds")}
    _run_state["skipped"] = (_run_state["skipped"] + [entry])[-20:]
    _run_state["skipped_total"] += 1
    log.warning("Sweep of %s (profile %s) SKIPPED: %s is still running. One SAP GUI "
                "desktop runs one sweep at a time -- if this repeats, the schedule "
                "is denser than one machine can serve.",
                requested, profile_id, entry["blocked_by"])
    threading.Thread(target=_append_run_history, daemon=True,
                     args=({"type": "skipped", **entry},)).start()
    _history_decision(requested, "skipped", trigger or _trigger_for(profile_id), profile_id,
                      planned_at, f"{entry['blocked_by'] or 'another run'} was still running")


def _trigger_for(profile_id, narrow_to_profile=None) -> str:
    """Run-history trigger for a run started by _run_one_system_background."""
    if narrow_to_profile:
        return "manual-profile"
    if profile_id is None:
        return "manual-system"
    if profile_id == "rca":
        return "rca"
    if str(profile_id).startswith("Retry"):
        return "retry"
    return "schedule"


def _history_decision(system: str, result: str, trigger: str, profile_id=None, planned_at=None,
                      reason: str = "") -> None:
    """A scheduling decision for the run history. Never raises."""
    try:
        from core import run_history
        nxt = None
        try:
            nxt = (next_runs().get("systems") or {}).get(system)
        except Exception:  # noqa: BLE001
            nxt = None
        run_history.record_decision(
            system, result, trigger=trigger,
            profile_id=None if profile_id in (None, "rca") else profile_id,
            planned_at=planned_at, reason=reason,
            next_run_at=(nxt or {}).get("at"),
            next_run_reason=("parked until cleared" if result == "parked" else
                             "schedule" if nxt else None))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"run history decision not written: {exc}")


_run_lock = threading.Lock()
_run_history_lock = threading.Lock()
# A manual run is claimed here BEFORE the reply: six operators pressing
# "Monitor this system" at once all got {"started": true} while only one ran
# (the real check happened later, inside the thread).
_manual_claim_lock = threading.Lock()
_manual_claim = {"system": None, "at": 0.0}


def _claim_manual_run(label: str) -> None:
    """409 when a run is going or another manual run was just started."""
    with _manual_claim_lock:
        pending = _manual_claim["system"]
        if pending and time_module.monotonic() - _manual_claim["at"] > 120:
            pending = None                       # its thread never got going
        if _run_state["running"] or pending:
            what = _run_state.get("current_system") or pending or "all systems"
            raise HTTPException(status_code=409, detail=f"A run is already in progress ({what}).")
        _manual_claim.update(system=label, at=time_module.monotonic())


def _release_manual_claim() -> None:
    with _manual_claim_lock:
        _manual_claim.update(system=None, at=0.0)


def _claimed(target):
    """Run `target` in the thread, releasing the claim once it has taken (or failed to take) the run."""
    def _go(*a, **k):
        try:
            return target(*a, **k)
        finally:
            _release_manual_claim()
    return _go
# manual_pause: a manual run (Monitor this system / Run profile / Run all)
# is in progress. It HOLDS automatic monitoring for its duration without
# touching `enabled`, and clears itself when the manual run ends -- so a
# manual run can no longer leave automatic monitoring switched off.
_scheduler_state = {"last_cycle_start": None, "next_run_at": None, "enabled": True,
                    "snooze_until": None, "manual_pause": False,
                    "resume_after_manual": False}


@app.get("/healthz")
def healthz():
    """Process liveness endpoint. Does not require SAP/AI connectivity."""
    return liveness()


@app.get("/api/services")
def api_services():
    """Heartbeat age of each service and the job queue, for the UI and uptime checks."""
    from core import store
    return {"role": process_role(), "heartbeats": store.heartbeats(), "jobs": store.jobs_summary()}


@app.get("/api/jobs")
def api_jobs(limit: int = 50):
    from core import store
    return {"summary": store.jobs_summary(), "recent": store.recent_jobs(min(max(limit, 1), 500))}


_watchdog_last = {"at": 0.0}


def _web_watchdog():
    """The web service watches the others (the jobs runner watches the web)."""
    if process_role() == "collector":
        return
    now = time_module.monotonic()
    if now - _watchdog_last["at"] < 60:
        return
    _watchdog_last["at"] = now
    try:
        from services.jobs import check_heartbeats
        check_heartbeats("web")
    except Exception as exc:
        log.warning(f"web watchdog failed: {exc}")


@app.get("/readyz")
def readyz():
    """Local readiness endpoint with safe, non-secret diagnostics."""
    _web_watchdog()
    result = readiness()
    try:
        from core import store
        result["services"] = store.heartbeats()
    except Exception:
        pass
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
        # Not configured here: it may be collected by another dashboard
        # (config/remote_sources.yaml). Answer from that dashboard's data.
        try:
            from dashboard import remote_sources
            remote = remote_sources.find(system_name)
        except Exception:  # noqa: BLE001 -- fall through to the normal 404
            remote = None
        if remote is not None:
            return remote
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
        return _grade_live(payload)
    payload = dict(payload)
    payload["cache_age_seconds"] = _cached_live_age(system_name)
    return _grade_live(payload)


def _grade_live(payload: dict) -> dict:
    """Wall payload graded with the system's own limits (core/wall_grades.py). Never raises."""
    try:
        from core.wall_grades import grade_live_payload
        return grade_live_payload(payload)
    except Exception as exc:  # noqa: BLE001 -- an ungraded card beats no card
        log.debug(f"grading {payload.get('system')} failed: {exc}")
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
        # Carry the customer so a dashboard that shows these cards remotely
        # (remote_sources.py) can apply its own users' customer scope.
        try:
            payload = dict(payload)
            payload.setdefault("customer", str(cfg.get("customer") or ""))
        except Exception:
            pass
        # Graded at serve time with this system's own limits (Thresholds
        # page), so a changed limit shows on the next poll of the page.
        payload = _grade_live(payload)
        out.append(payload)

    # Systems collected by other dashboards (config/remote_sources.yaml). A
    # failure here must never take the local wall down.
    try:
        from dashboard import remote_sources
        out = remote_sources.merge(out)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"remote sources merge failed: {type(exc).__name__}: {exc}")

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
    for fn in ("shell.js", "shell.css", "ops.js"):
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
    html = re.sub(r"((?:shell|ops)\.(?:js|css))\?v=[A-Za-z0-9_.-]+", rf"\1?v={ver}", html)
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

def process_role() -> str:
    """
    IBO_ROLE splits the one process into two without a rewrite:

        all        (default) UI + live reads + scheduler + sweeps, as before
        ui         UI and live RFC reads only. Never sweeps. A GUI hang or a
                   crash in the collector cannot take the wall down.
        collector  Scheduler and sweeps (and the same UI, usually on another
                   port, for the operator who wants to watch it).

    Run two processes on the Windows VM -- packaging/windows/start_ui.ps1
    and start_collector.ps1 -- and point the wall at the UI one.
    """
    role = (os.getenv("IBO_ROLE", "all") or "all").strip().lower()
    return role if role in ("all", "ui", "collector") else "all"


def sweeps_allowed() -> bool:
    return process_role() != "ui"


def require_collector_role():
    if not sweeps_allowed():
        raise HTTPException(
            status_code=409,
            detail="This instance runs with IBO_ROLE=ui and never sweeps. Start the "
                   "sweep on the collector instance (IBO_ROLE=collector).")
    return True


def _error_ref(exc: BaseException, where: str) -> str:
    """
    Record an internal error server-side and return only a reference for
    the browser. Exception text can carry file paths, hosts or credential
    fragments; it goes to the log / Errors page, never into a response.
    """
    err_id = None
    try:
        from core import errors as _errors
        err_id = _errors.capture(exc, "web", context=where, notify=False)
    except Exception:
        err_id = None
    err_id = err_id or secrets.token_hex(4)
    log.warning(f"{where} failed: {type(exc).__name__}: {exc} (error id {err_id})")
    return f"error id {err_id}"


def _internal_error(exc: BaseException, where: str) -> str:
    return f"Internal error ({_error_ref(exc, where)}). Details are in the server log."


def require_admin():
    """Admin -- or a person core/access.decide allowed for this system (manager / team leader /
    granted consultant changing a system of his project; the middleware checked it)."""
    from core import auth
    if not auth.may_configure():
        raise HTTPException(status_code=403, detail="admin role required")
    return True


def require_token(x_ibo_token: str = Header(default="")):
    """
    Defence in depth behind _auth_middleware: a state-changing call needs a
    principal with at least the operator role (session + CSRF, loopback
    local admin + CSRF, or the Bearer API token -- all verified by the
    middleware, which also enforces admin-only routes). The header argument
    is kept for signature compatibility and is not trusted here.
    """
    from core import auth
    if auth.may_write():
        return True
    if auth.current_user.get() is None:
        raise HTTPException(status_code=401, detail="login required")
    raise HTTPException(status_code=403, detail="operator role required")


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


def _enabled_schedule_count() -> int:
    """How many configured systems have a saved, enabled Scheduler-page schedule."""
    try:
        from core.schedules import load_schedules
        stored = load_schedules()
        return sum(1 for s in get_systems()
                   if (stored.get(s.get("name")) or {}).get("enabled"))
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
    from collectors.rfc_collector import SapSession
    reset_capabilities(system or None)
    SapSession.forget_missing_functions(system or None)
    return {"reset": system or "all systems"}


@app.get("/api/parser-health")
def api_parser_health():
    """Per system and T-code: how many fields extraction yields now vs its best."""
    from core import parser_health
    return parser_health.snapshot()


@app.post("/api/parser-health/reset", dependencies=[Depends(require_token)])
def api_parser_health_reset(system: str = "", tcode: str = ""):
    """After changing ocr_patterns.yaml on purpose, forget the old baseline."""
    from core import parser_health
    parser_health.reset(system or None, tcode or None)
    return {"reset": system or "all", "tcode": tcode or "all"}


@app.get("/api/alerts")
def api_alerts(system: str = "", limit: int = 100):
    """
    What was mailed, what was held back, and why -- today's totals plus the
    most recent entries. One place to answer "did anyone get told about PS4".
    """
    from core import alert_ledger, auth
    # Only the caller's systems; recipients only for users who may act
    # (security review 28.10.2026: this returned every customer's mail log).
    scoped = _user_is_scoped()
    allowed = {s.get("name") for s in get_systems()}
    if scoped and system and system not in allowed:
        raise HTTPException(status_code=404, detail="Unknown system")
    n = min(max(limit, 1), 500)
    rows = alert_ledger.recent(limit=n if (system or not scoped) else max(n * 5, 500), system=system or None)
    if scoped:
        rows = [r for r in rows if r.get("system") in allowed][:n]
    if not auth.may_write():
        rows = [dict(r, recipients="") if isinstance(r, dict) else r for r in rows]
    today = alert_ledger.today_summary()
    if scoped:
        today = {k: v for k, v in today.items() if k in allowed}
    return {"today": today, "recent": rows}


@app.get("/api/reach-test")
def api_reach_test(host: str, port: int):
    """
    Can THIS machine open host:port right now? Used by a dashboard that
    reaches SAP systems through a tunnel to here, to tell "the system is
    down" from "the tunnel refused to forward". Limited to private
    addresses and SAP ports (32xx/33xx/36xx) so it cannot be used as a
    general port scanner. Login or X-IBO-Token required like every /api.
    """
    import ipaddress
    from core.reachability import tcp_open
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(status_code=400, detail="host must be an IP address")
    if not ip.is_private or not (3200 <= port <= 3399 or 3600 <= port <= 3699):
        raise HTTPException(status_code=403, detail="only private addresses and SAP ports")
    return {"host": host, "port": port, "reachable": tcp_open(host, port, 4)}


@app.get("/api/vpn-status/{name}")
def api_vpn_status(name: str):
    """
    This machine's ibops-vpn watchdog status (packaging/vpn), for a dashboard
    that reaches systems THROUGH this machine (remote: check in
    core/reachability.py). Login or X-IBO-Token required like every /api.
    """
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name or "") or ".." in name:
        raise HTTPException(status_code=404, detail="no such VPN watchdog")   # no paths (review 28.10.2026)
    from core.reachability import watchdog_status
    st = watchdog_status(name)
    if st is None:
        raise HTTPException(status_code=404, detail=f"no VPN watchdog named {name} on this machine")
    return st


@app.get("/api/capabilities")
def api_capabilities():
    """Which function modules each system was found not to have, and since when."""
    from collectors.rfc_collector import SapSession
    from datetime import datetime as _dt
    with SapSession._failures_lock:
        SapSession._missing_load()
        items = dict(SapSession._missing_fms)
    out: dict = {}
    vis = _visible_names()
    for (system, fm), ts in sorted(items.items()):
        if vis is not None and system not in vis:
            continue
        out.setdefault(system, {})[fm] = {"since": _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")}
    return {"missing_function_modules": out}


@app.get("/api/delivery-accounts")
def api_delivery_accounts():
    """Named sender accounts. Secrets reported as configured, never returned."""
    from core.delivery_accounts import load_accounts
    return load_accounts()


@app.post("/api/delivery-accounts", dependencies=[Depends(require_token), Depends(require_admin)])
def api_save_delivery_accounts(payload: dict):
    from core.delivery_accounts import save_accounts
    try:
        return {"saved": True,
                "accounts": save_accounts(payload.get("accounts") or {},
                                          payload.get("secrets"))}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_internal_error(exc, "save delivery accounts"))


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


@app.post("/api/delivery-settings", dependencies=[Depends(require_token), Depends(require_admin)])
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
        raise HTTPException(status_code=500, detail=_internal_error(exc, "save delivery settings"))


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
    from core.profiles import save_profiles, status, _load_raw
    from core import auth, projects

    profiles_in, alerts_in = payload.profiles, payload.alerts
    me = auth.current_user.get() or {}
    every = me.get("role") == "admin" or me.get("via") in ("local", "token") or (
        me.get("customers") is None and me.get("role") in ("manager", "team_lead", "operator"))
    if not every:
        # A person limited to some projects changes only the profiles of systems
        # he may change; everyone else's profiles and the alert routing are kept.
        member = projects.membership()
        for p in profiles_in:
            if not projects.may(me, "settings", str(p.get("system") or ""), member):
                raise HTTPException(status_code=403, detail=f"Profile {p.get('id')}: you may not change "
                                                              f"{p.get('system') or 'that system'}.")
        kept = [p for p in (_load_raw().get("profiles") or [])
                if not projects.may(me, "settings", str(p.get("system") or ""), member)]
        clash = {str(p.get("id")) for p in kept} & {str(p.get("id")) for p in profiles_in}
        if clash:
            raise HTTPException(status_code=403, detail=f"Profile id already used elsewhere: {', '.join(sorted(clash))}")
        profiles_in = kept + list(profiles_in)
        alerts_in = None                                   # routing covers everyone: unchanged
    try:
        save_profiles(profiles_in, alerts_in)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_internal_error(exc, "save profiles"))

    return {"saved": True, "profiles": status()}


@app.post("/api/profiles/alerts", dependencies=[Depends(require_token)])
def api_save_alert_routing(payload: dict):
    from core.profiles import alert_routing, save_alerts
    try:
        save_alerts(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_internal_error(exc, "save alert routing"))
    return {"saved": True, "alerts": alert_routing()}


@app.get("/api/profiles")
def api_profiles():
    """Every monitoring profile with its schedule, T-codes and destinations."""
    from core.profiles import alert_routing, status
    from core import auth
    profiles = status()
    vis = _visible_names()
    if vis is not None:            # black-box test 28.10.2026: every project's profiles leaked
        profiles = [p for p in profiles if p.get("system") in vis]
    if not auth.may_write() or vis is not None:
        profiles = [dict(p, email_to=[], email_cc=[], recipients_hidden=True) for p in profiles]
    return {"profiles": profiles, "alerts": alert_routing()}     # channels only, no addresses


@app.post("/api/profiles/{profile_id}/run",
          dependencies=[Depends(require_token), Depends(require_collector_role)])
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

    if not any(s.get("name") == profile["system"] for s in get_systems()):
        raise HTTPException(status_code=404,
                            detail=f"Unknown system: {profile['system']}")
    _claim_manual_run(profile["system"])          # 409 if a run is going or just started

    # Same rules as "Monitor this system": automatic monitoring is held for
    # the duration and the hold ends by itself with the run.
    _begin_manual_hold()
    thread = threading.Thread(target=_claimed(_run_one_system_background),
                              args=(profile["system"],), kwargs={"narrow_to_profile": profile_id},
                              daemon=True)
    thread.start()

    # Captured: the system's T-codes (Profile page card), narrowed to this
    # profile's own T-codes when it names them.
    return {"started": True, "scope": profile["system"],
            "profile": profile_id,
            "delivers": profile["deliver"],
            "tcodes_selected": tcodes,
            "note": ("The system's ticked T-codes are captured."
                     if profile.get("tcodes") == "all" else
                     "Only this profile's T-codes that are ticked for the system are captured.")}


@app.get("/api/tcode-sets")
def api_tcode_sets(system: str = ""):
    """The T-codes of one system: order, ticks and settings (Profile page card)."""
    from core import tcode_sets
    names = [s.get("name") for s in get_systems() if s.get("name")]
    name = system if system in names else (names[0] if names else "")
    return {"system": name, "systems": names, "items": tcode_sets.for_system(name) if name else []}


@app.post("/api/tcode-sets", dependencies=[Depends(require_token)])
def api_save_tcode_set(payload: dict):
    from core import tcode_sets
    name = str((payload or {}).get("system") or "")
    if not any(s.get("name") == name for s in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {name}")
    try:
        items = tcode_sets.save(name, (payload or {}).get("items") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"saved": True, "system": name, "items": items}


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")


@app.get("/api/auth-token")
def api_auth_token():
    """
    Gone. This used to hand IBO_API_TOKEN (an admin credential) to the
    browser -- and behind a reverse proxy on the same host every request
    looks like loopback, so it handed it to everyone. Browsers now write
    with their session + X-CSRF-Token (see /api/me); the API token is for
    non-browser automation only, as `Authorization: Bearer <token>`.
    """
    return JSONResponse(status_code=410, content={
        "detail": "Removed. The dashboard uses your login session; automation sends "
                  "'Authorization: Bearer <IBO_API_TOKEN>'.", "token": ""})


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
            errors.append(f"{name}: could not read incidents ({_error_ref(exc, 'incidents ' + str(name))})")

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
            errors.append(f"{name}: could not read events ({_error_ref(exc, 'events ' + str(name))})")

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
        return {"rules": [], "error": f"Could not load correlation rules ({_error_ref(exc, 'correlation rules')})"}

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


@app.get("/api/logon-blocks")
def logon_blocks():
    """
    RFC/DB logon blocks set by the cross-process logon guard
    (collectors/rfc_logon_guard.py), limited to the caller's customer scope:
    system, reason (invalid_password | locked | expired), since, next retry.
    """
    from collectors import rfc_logon_guard as _guard
    allowed = {s.get("name") for s in get_systems()}
    out = []
    for b in _guard.list_blocks():
        if b.get("system") not in allowed:
            continue
        out.append({"system": b.get("system"), "kind": b.get("kind"), "user": b.get("user"),
                    "client": b.get("client"), "reason": b.get("reason"),
                    "since": b.get("blocked_at"), "next_retry_at": b.get("next_retry_at"),
                    "error": b.get("error"),
                    "message": _guard.blocked_message(b.get("system"), b)})
    return {"blocks": out}


@app.post("/api/logon-blocks/{system_name}/clear", dependencies=[Depends(require_token)])
def logon_block_clear(system_name: str):
    """
    Admin action: allow RFC/DB logons for this system again (one new attempt).
    At most once per system every IBO_LOGON_CLEAR_COOLDOWN_MIN (30) minutes:
    each clear with an unchanged wrong password costs one failed SAP logon,
    and SAP locks the user after a few. A changed password in .env lifts the
    block by itself, without this button.
    """
    from collectors import rfc_logon_guard as _guard
    from core import auth
    if system_name not in {s.get("name") for s in get_systems()}:
        raise HTTPException(status_code=404, detail=f"unknown system '{system_name}'")
    try:
        cool = max(0, int(os.getenv("IBO_LOGON_CLEAR_COOLDOWN_MIN", "30") or 30))
    except ValueError:
        cool = 30
    now = time_module.monotonic()
    last = _logon_clears.get(system_name)
    if cool and last is not None and now - last < cool * 60:
        wait = int((cool * 60 - (now - last)) // 60) + 1
        raise HTTPException(status_code=429, detail=(
            f"The logon block for {system_name} was cleared {int((now - last) // 60)} min ago. "
            f"Wait {wait} min, or fix the password in .env (that lifts the block by itself) -- "
            f"each clear with a wrong password costs one failed SAP logon and can lock the user."))
    _logon_clears[system_name] = now
    n = _guard.clear_logon_block(system_name)
    who = (auth.current_user.get() or {}).get("username", "?")
    log.warning(f"Logon block for {system_name} cleared from the dashboard by {who} ({n} entr"
                f"{'y' if n == 1 else 'ies'}).")
    return {"system": system_name, "cleared": n}


_logon_clears: dict = {}          # system -> monotonic time of the last manual clear

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
        systems = [c for c in get_systems() if _standby_active(c)]
        _refresh_run.update(in_progress=True,
                            started_at=datetime.now().strftime("%H:%M:%S"),
                            finished_at=None, done=0, total=len(systems),
                            error=None)

    # Systems shown from other collectors (config/remote_sources.yaml) are
    # re-read on THEIR machine as part of the same button press, so the wall
    # never shows a collector's stale answer after "Read live".
    try:
        from dashboard import remote_sources as _rs
        _remote_count = len(_rs.load_sources())
    except Exception:  # noqa: BLE001
        _rs, _remote_count = None, 0
    with _refresh_run_lock:
        _refresh_run["total"] += _remote_count

    def _run():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            workers = min(int(os.environ.get("IBO_LIVE_WORKERS", "3") or 3),
                          max(1, len(systems)))
            pool = ThreadPoolExecutor(max_workers=workers + (1 if _remote_count else 0),
                                      thread_name_prefix="ondemand-rfc")
            futures = [pool.submit(_refresh_one_live, cfg) for cfg in systems]
            if _remote_count:
                futures.append(pool.submit(_refresh_remote_logged))
            for f in as_completed(futures):
                with _refresh_run_lock:
                    _refresh_run["done"] += _remote_count if f is futures[-1] and _remote_count else 1
            pool.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001 -- report, never crash the thread
            with _refresh_run_lock:
                _refresh_run["error"] = f"Live refresh failed ({_error_ref(exc, 'live refresh')})"
        finally:
            with _refresh_run_lock:
                _refresh_run["in_progress"] = False
                _refresh_run["finished_at"] = datetime.now().strftime("%H:%M:%S")

    threading.Thread(target=_run, daemon=True, name="ondemand-refresh").start()
    with _refresh_run_lock:
        return {"started": True, **_refresh_run}


def _refresh_remote_logged():
    from dashboard import remote_sources
    for note in remote_sources.refresh_remote():
        log.info(f"Read live (remote): {note}")


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
        payload = _run_in_rfc_pool(read_live_job, system_name, cfg)
    else:
        from collectors.rfc_live import read_live
        payload = read_live(system_name, cfg, use_cache=False)

    if not payload.get("connected", False):
        # Refuse rather than analyse nothing. Asking a model to explain an
        # absence of data produces confident prose about a system it never
        # saw, which is worse than no answer.
        try:
            from core import rca_log
            rca_log.not_started(system_name, "analyse now (AI RCA page)",
                                "the system did not answer over RFC: " + str(payload.get("error") or "")[:200])
        except Exception:  # noqa: BLE001
            pass
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
        attribution = {"gaps": [f"attribution failed ({_error_ref(exc, 'RCA attribution')})"]}

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
        error = f"AI narrative failed ({_error_ref(exc, 'RCA narrative ' + system_name)})"

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

    try:
        from core import rca_log
        rid = rca_log.start(system_name, "analyse now (AI RCA page)", "analyse-now")
        rca_log.finish(rid, "done" if not error else "done (no AI narrative)", {
            "severity": result.overall_status.value,
            "headline": (detail or {}).get("root_cause") or (
                f"{len([f for f in findings if f['status'] in ('CRITICAL', 'WARNING')])} value(s) outside "
                f"threshold" if findings else "nothing outside threshold"),
            "root_cause": (detail or {}).get("root_cause") or "",
            "culprits": [{"type": "value", "name": f["name"], "evidence": f["value"], "impact": f.get("detail") or ""}
                         for f in findings if f["status"] in ("CRITICAL", "WARNING")][:8],
            "source": getattr(provider, "name", "") if not error else "rules (model unavailable)"},
            mail_result="not mailed: shown on screen only", error=error or "")
    except Exception:  # noqa: BLE001
        pass
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


@app.get("/api/rca/log")
def api_rca_log(days: int = 7, system: str = ""):
    """Every RCA: when, why, result, the cause and culprits found, who was mailed (core/rca_log.py)."""
    from core import rca_log
    allowed = {s.get("name") for s in get_systems()}
    rows = [r for r in rca_log.query(max(1, min(int(days), 90)), system or None) if r["system"] in allowed]
    from utils.display_time import to_display
    for r in rows:
        r["started_display"] = to_display(r.get("started_at"))
    from core import auth
    if not auth.may_write():                  # read-only roles see how many, not the addresses
        for r in rows:
            n = len([x for x in (r.get("mailed_to") or "").split(",") if x.strip()])
            r["mailed_to"] = f"{n} person(s)" if n else ""
    return {"runs": rows}


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
        provider_status = {"error": f"AI provider unavailable ({_error_ref(exc, 'AI provider status')})"}

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

    # build_report() asks the AI provider for a summary paragraph. Without a
    # cache every page load (and every open tab's refresh) was a model call
    # for the same, unchanged snapshot. Keyed by the snapshot's own stamp
    # (and metric count), so a new sweep produces a new report.
    key = (system_name, str(stamp), len(result.metrics), str(result.overall_status))
    with _report_cache_lock:
        cached = _report_cache.get(key)
    if cached is not None:
        return cached
    report = build_report(result)
    with _report_cache_lock:
        _report_cache[key] = report
        while len(_report_cache) > 64:
            _report_cache.pop(next(iter(_report_cache)))
    return report


_report_cache: dict = {}
_report_cache_lock = threading.Lock()


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


@app.post("/api/systems", dependencies=[Depends(require_token), Depends(require_admin)])
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
        _begin_manual_hold()
        thread = threading.Thread(target=_run_one_system_background,
                                  args=(system.name,), kwargs={"trigger": "new-system"},
                                  daemon=True)
        thread.start()
        started = True

    return {"success": True, "monitoring_started": started, "warnings": warnings}


@app.delete("/api/systems/{system_name}", dependencies=[Depends(require_token), Depends(require_admin)])
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
        **_run_progress(),
        "last_profile": _scheduler_state.get("last_profile"),
        # Scheduled runs come from the Profiles page AND from the per-system
        # schedules saved on the Scheduler page. interval_minutes is legacy
        # and nothing runs on it.
        "source": "profiles",
        "enabled_profiles": _enabled_profile_count(),
        "enabled_schedules": _enabled_schedule_count(),
        # Held while a manual run is in progress (ends by itself with it).
        "manual_hold": bool(_scheduler_state.get("manual_pause")),
        # systems.yaml cannot be parsed: the last good copy is being served.
        "config_error": _config_error(),
        "scheduler_loop_error": _scheduler_state.get("loop_error"),
    }


def _config_error():
    try:
        from core.config_loader import config_error
        return config_error()
    except Exception:  # noqa: BLE001
        return None


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
    _require_unscoped("Snoozing automatic monitoring")
    try:
        minutes = max(1, min(int(minutes), 60 * 24 * 30))   # cap at 30 days
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="minutes must be a number")

    until = datetime.now() + timedelta(minutes=minutes)
    _scheduler_state["snooze_until"] = until
    _scheduler_state["next_run_at"] = None
    _scheduler_state["paused_info"] = (f"snoozed at {datetime.now():%d.%m %H:%M} until "
                                       f"{until:%d.%m %H:%M}")
    # A manual run's hold is separate: it ends with the run, and
    # snooze_until then holds the scheduler off until the snooze expires.
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

    This is in-memory only: a restart returns to the SCHEDULER_ENABLED
    setting in .env (and the startup catch-up rule). That is deliberate -- a pause is a
    temporary act, and silently persisting it would leave monitoring off for
    weeks with nobody remembering why.
    """
    _require_unscoped("Pausing or resuming automatic monitoring")
    action = action.strip().lower()
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=400,
                            detail="Use /api/scheduler/pause or /api/scheduler/resume.")

    # A manual run in progress holds automatic monitoring by itself
    # (manual_pause) and releases it when it ends; Pause / Resume only set
    # what happens after that, so they act on `enabled` directly.
    _scheduler_state["enabled"] = (action == "resume")
    _scheduler_state["resume_after_manual"] = False
    _scheduler_state["paused_info"] = (f"paused with the Pause button at {datetime.now():%d.%m %H:%M}"
                                       if action == "pause" else None)
    # Clear any snooze: leaving one in place would mean "Resume" appeared to
    # work while nothing actually ran until the snooze expired.
    _scheduler_state["snooze_until"] = None
    log.info(f"Automatic monitoring {'resumed' if action == 'resume' else 'paused'} "
             f"from the dashboard.")

    return {
        "enabled": _scheduler_state["enabled"],
        "running": bool(_run_state["running"]),
        "note": ("A run already in progress will finish."
                 if _run_state["running"] and action == "pause"
                 else "Automatic monitoring continues when the manual run finishes."
                 if _scheduler_state.get("manual_pause") else ""),
        "next_run_at": _scheduler_state["next_run_at"] if _scheduler_state["enabled"] else None,
    }


def next_runs(now: datetime | None = None) -> dict:
    """
    When each system is next monitored automatically (Scheduler-page schedule
    or a Profiles-page profile, whichever is first), and what would stop it:
    automatic monitoring paused or snoozed, or the system parked / in
    maintenance. For the Live wall.
    """
    from core import schedules as _sch
    now = now or datetime.now()
    stored = _sch.load_schedules()
    out: dict = {}

    def _add(system, when, what):
        if not system or not when:
            return
        cur = out.get(system)
        if cur is None or when < cur["_at"]:
            out[system] = {"_at": when, "what": what}

    for cfg in get_systems():
        name = cfg.get("name")
        sc = stored.get(name)
        try:
            nxt = _sch.planned_next(sc, _sch.handled_until(name), now) if sc else None
        except Exception:  # noqa: BLE001
            nxt = None
        _add(name, nxt, _sch.describe(sc) if sc else "")
    try:
        from core.profiles import status as _profile_status
        for p in _profile_status(now):
            if p.get("next_run") and (p.get("schedule") or {}).get("enabled"):
                try:
                    when = datetime.fromisoformat(str(p["next_run"]))
                except ValueError:
                    continue
                _add(p.get("system"), when, f"profile {p.get('id')} ({p.get('schedule_text', '')})")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"next runs: profiles not read: {exc}")

    snooze = _scheduler_state.get("snooze_until")
    snoozed = bool(snooze and now < snooze)
    paused = not _scheduler_state.get("enabled")
    general = (f"snoozed until {snooze:%H:%M}" if snoozed else
               "automatic monitoring is paused -- press Resume on the Scheduler page" if paused else None)
    systems = {}
    first = None
    for name, v in out.items():
        at = v["_at"]
        if at < now:
            at = now                                   # due now: starts on the next pass
        held = general
        if not held:
            block = _sweep_block_reason(name)
            if not block:
                # A Stop holds automatic runs until the next slot; only a slot
                # BEFORE the hold ends would be skipped.
                until = _stop_hold.get(name)
                if until and until > at:
                    block = _stop_hold_reason(name)
            if block:
                held = f"will be skipped: {block}"
        day = ("today" if at.date() == now.date() else
               "tomorrow" if at.date() == (now + timedelta(days=1)).date() else f"{at:%d %b}")
        systems[name] = {"at": at.strftime("%Y-%m-%d %H:%M"), "text": f"{day} {at:%H:%M}",
                         "what": v["what"], "held": held}
        if not held and (first is None or at < first[1]):
            first = (name, at)
    return {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "paused": paused, "snoozed": snoozed,
            "held": general, "systems": systems,
            "next": ({"system": first[0], "at": first[1].strftime("%Y-%m-%d %H:%M"),
                      "text": systems[first[0]]["text"]} if first else None)}


@app.get("/api/schedules/next")
def api_next_runs():
    """Next automatic run per system, for the Live wall."""
    d = next_runs()
    vis = _visible_names()
    if vis is not None:
        d = dict(d, systems={k: v for k, v in (d.get("systems") or {}).items() if k in vis})
        if d.get("next") and d["next"].get("system") not in vis:
            firsts = sorted(((v.get("at") or "9999"), k) for k, v in d["systems"].items() if v.get("at"))
            d["next"] = ({"system": firsts[0][1], "at": firsts[0][0], "text": d["systems"][firsts[0][1]].get("text")}
                         if firsts else None)
    return d


@app.get("/api/schedules")
def get_all_schedules():
    """
    Every configured system with its schedule, description and next run.

    A system with no SAVED schedule is "not scheduled" and never runs; the
    form is pre-filled with DEFAULT but shown unticked. `last_run` is the
    system's last sweep of any kind (from its snapshot); `next_run` is when
    the scheduler will next start it.
    """
    from core.schedules import (load_schedules, describe, planned_next, DEFAULT,
                                handled_until as schedule_last_run)

    stored = load_schedules()
    now = datetime.now()
    out = []
    for cfg in get_systems():
        name = cfg.get("name")
        saved = stored.get(name)
        sched = saved or {**DEFAULT, "enabled": False}

        snap = load_snapshot(name) or {}
        last = snap.get("cycle_timestamp")

        nxt = planned_next(saved, schedule_last_run(name), now) if saved else None
        out.append({
            "system": name,
            "sid": cfg.get("sap_system_id") or name,
            "schedule": sched,
            "scheduled": bool(saved),
            "description": describe(sched) if saved else "not scheduled",
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
    name: str = ""               # e.g. "Full sweep" -- shown in the Monitoring log


@app.post("/api/schedules/{system_name}", dependencies=[Depends(require_token)])
def update_schedule(system_name: str, body: ScheduleUpdate):
    from core.schedules import set_schedule, describe, planned_next, set_handled

    if not any(s.get("name") == system_name for s in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")

    saved = set_schedule(system_name, body.dict())
    # Slots before the save are handled: saving never starts a sweep by
    # itself. "every 2 h" (grid through 08:00) saved at 11:30 first runs at
    # 12:00; "daily at 08:00" saved at 07:50 still runs at 08:00 today.
    now = datetime.now()
    set_handled(system_name, now)
    log.info(f"Schedule updated for {system_name}: {describe(saved)}")
    nxt = planned_next(saved, now, now)

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
    if not 5 <= settings.interval_minutes <= 10080:
        raise HTTPException(status_code=400, detail="Interval must be 5-10080 minutes (one week).")
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
            try:
                _check_report_scope(f"{day}/{fname}")
            except HTTPException:
                continue
            results.append({"date": day, "filename": fname, "url": f"/reports/{day}/{fname}"})
    return results[:10]


def _append_run_history(event: dict):
    # Thread lock for this process, file lock for the other one
    # (python main.py writes the same file), atomic replace for readers.
    try:
        from core import timeline as _tl
        if event.get("system"):
            _tl.add(event["system"], "sweep", event.get("event") or "Sweep",
                    severity="ok" if event.get("status") == "ok" else "warning")
        elif event.get("type") == "skipped" and event.get("requested"):
            _tl.add(str(event["requested"]), "sweep", "Sweep request dropped",
                    f"{event.get('blocked_by')} was still running", severity="warning")
    except Exception:
        pass
    from core.atomic_json import locked, read_json, write_json
    with _run_history_lock, locked(RUN_HISTORY_PATH):
        history = read_json(RUN_HISTORY_PATH, default=[])
        if not isinstance(history, list):
            history = []
        history.append(event)
        history = history[-50:]
        try:
            write_json(RUN_HISTORY_PATH, history)
        except Exception as e:
            log.error(f"Failed to write run history: {e}")


def _visible_names() -> set | None:
    """The caller's systems, or None when the caller sees every system."""
    return {s.get("name") for s in get_systems()} if _user_is_scoped() else None


def _user_is_scoped() -> bool:
    from core import auth
    user = auth.current_user.get()
    # Limited by project OR by per-person grants (customers '*' with a systems
    # subset was treated as unscoped -- security review 28.10.2026).
    return bool(user and (user.get("customers") is not None or user.get("systems") is not None))


def _scope_rows(rows: list) -> list:
    """Run-history rows of the caller's systems only; a 'blocked by' that names
    another customer's system is blanked (it said whose sweep was running)."""
    allowed = {s.get("name") for s in get_systems()}
    out = []
    for r in rows:
        if r.get("system") not in allowed:
            continue
        out.append(_hide_foreign_names(r, allowed))
    return out


def _hide_foreign_names(row: dict, allowed: set) -> dict:
    """Replace other customers' system names inside a row's texts (e.g. a skip
    reason "BETA_PRD was still running") with 'another system'."""
    try:
        from core.config_loader import get_all_systems
        foreign = sorted((s.get("name") for s in get_all_systems()
                          if s.get("name") and s.get("name") not in allowed), key=len, reverse=True)
    except Exception:  # noqa: BLE001
        foreign = []
    if not foreign:
        return row
    row = dict(row)
    for k, v in row.items():
        if k == "system" or not isinstance(v, str):
            continue
        for name in foreign:
            if name in v:
                v = v.replace(name, "another system")
        row[k] = v
    return row


def _require_unscoped(action: str) -> None:
    """Machine-wide controls (pause/snooze/resume all monitoring) are not for
    users limited to some customers: they would stop every customer's checks."""
    if _user_is_scoped():
        raise HTTPException(status_code=403, detail=(
            f"{action} affects every customer's monitoring; your login is limited to some customers. "
            "Ask an administrator, or use Stop on your own system's run."))


@app.get("/api/run-history")
def get_run_history(system: str = "", result: str = "", since: str = "", limit: int = 0,
                    trigger: str = ""):
    """
    Every run and scheduling decision (core/run_history.py), newest first.
    Read-only, like the other polled endpoints. Filters: system, result
    (comma list), since (ISO date/time), trigger, limit. Each row also has
    the old feed's fields (event, status, timestamp), so the start page's
    activity list keeps working; without filters it returns the last 20.
    """
    from core import run_history, run_names
    filtered = bool(system or result or since or trigger)
    n = limit or (200 if filtered else 20)
    if not _user_is_scoped():
        return run_names.add_names(run_history.query(system=system, result=result, since=since or None,
                                                     limit=n, trigger=trigger))
    rows = run_history.query(system=system, result=result, since=since or None,
                             limit=max(n * 10, 500), trigger=trigger)
    return run_names.add_names(_scope_rows(rows)[:n])


@app.get("/api/run-history.csv")
def get_run_history_csv(system: str = "", result: str = "", since: str = "", limit: int = 5000,
                        trigger: str = ""):
    from fastapi.responses import Response
    from core import run_history
    rows = run_history.query(system=system, result=result, since=since or None,
                             limit=limit or 5000, trigger=trigger)
    if _user_is_scoped():
        rows = _scope_rows(rows)
    return Response(content=run_history.to_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="run_history.csv"'})


@app.get("/api/run-history/next")
def get_run_history_next():
    """The next scheduled run of every profile and saved system schedule, and
    what would hold it (paused, snoozed, parked, maintenance)."""
    from core import schedules as _sch
    now = datetime.now()
    nr = next_runs(now)
    held_general = nr.get("held")
    out = []

    def _held(system: str) -> str | None:
        if held_general:
            return held_general
        block = _sweep_block_reason(system)
        if block and block.startswith("parked"):
            return "parked until cleared"
        return block

    try:
        from core.profiles import status as _profile_status
        for p in _profile_status(now):
            enabled = (p.get("schedule") or {}).get("enabled")
            out.append({"kind": "profile", "id": p.get("id"), "system": p.get("system"),
                        "name": p.get("label") or p.get("id"),
                        "schedule": p.get("schedule_text"), "enabled": bool(enabled),
                        "last_run": p.get("last_run"),
                        "next_run_at": (p.get("next_run") or "").replace("T", " ")[:16] if enabled else None,
                        "held": _held(p.get("system")) if enabled else "disabled"})
    except Exception as exc:  # noqa: BLE001
        log.debug(f"run history next: profiles not read: {exc}")
    try:
        stored = _sch.load_schedules()
        for cfg in get_systems():
            name = cfg.get("name")
            sc = stored.get(name)
            if not sc:
                continue
            nxt = _sch.planned_next(sc, _sch.handled_until(name), now)
            out.append({"kind": "schedule", "id": SCHEDULE_RUN_LABEL, "system": name,
                        "name": sc.get("name") or "",
                        "schedule": _sch.describe(sc), "enabled": bool(sc.get("enabled")),
                        "last_run": (_sch.last_run(name).isoformat(timespec="seconds")
                                     if _sch.last_run(name) else None),
                        "next_run_at": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
                        "held": _held(name) if sc.get("enabled") else "disabled"})
    except Exception as exc:  # noqa: BLE001
        log.debug(f"run history next: schedules not read: {exc}")
    out.sort(key=lambda r: (r["next_run_at"] is None, r["next_run_at"] or ""))
    return {"generated_at": now.strftime("%Y-%m-%d %H:%M:%S"), "paused": nr.get("paused"),
            "snoozed": nr.get("snoozed"), "manual_hold": bool(_scheduler_state.get("manual_pause")),
            "config_error": _config_error(),
            "items": [i for i in out if _visible_names() is None or i.get("system") in _visible_names()]}


@app.get("/run-history")
def run_history_page():
    return _page("run_history.html")


def _monitoring_log(days: int = 7, system: str = "", limit: int = 200, result: str = "") -> dict:
    """
    The Monitoring log (core/monitoring_log.py): next runs, runs with the mails
    that belong to them, and every mail decision in the period. Limited to the
    caller's customer scope; recipients only for admins (as on Alert history).
    """
    from core import alert_engine, auth, monitoring_log as ml, outbox, run_history
    try:
        days = max(1, min(int(days or 7), 90))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(1, min(int(limit or 200), 1000))
    except (TypeError, ValueError):
        limit = 200
    system = str(system or "").strip()
    user = auth.current_user.get()
    scoped = bool(user and user.get("customers") is not None)
    visible = [s.get("name") for s in get_systems() if s.get("name")]
    if scoped and system and system not in visible:
        raise HTTPException(status_code=404, detail="Unknown system")
    since = datetime.now() - timedelta(days=days)

    runs = run_history.query(system=system, result=result, since=since,
                             limit=limit if not scoped else 5000)
    if scoped:
        allowed = set(visible)
        runs = [_hide_foreign_names(r, allowed) for r in runs if r.get("system") in allowed][:limit]
    try:
        sends = alert_engine.history(system or None, days, limit=5000,
                                     systems=visible if scoped else None)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"monitoring log: alert history not readable: {exc}")
        sends = []
    emails = sorted((ml.email_from_send(r) for r in sends),
                    key=lambda m: str(m.get("at") or ""), reverse=True)
    try:
        entries = [e for e in outbox.recent(limit=2000)
                   if (not system or e.get("system") == system)
                   and (not scoped or e.get("system") in set(visible))]
    except Exception as exc:  # noqa: BLE001
        log.debug(f"monitoring log: outbox not readable: {exc}")
        entries = []
    runs = ml.link(runs, emails, entries, outbox.timeout_seconds())

    admin = auth.is_admin()
    if not admin:
        for m in emails + [m for r in runs for m in r["emails"]]:
            m["to"], m["cc"] = [], []

    try:
        nxt = get_run_history_next()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"monitoring log: next runs not read: {exc}")
        nxt = {"items": []}
    upcoming = []
    for it in nxt.get("items") or []:
        if scoped and it.get("system") not in set(visible):
            continue
        if system and it.get("system") != system:
            continue
        held = it.get("held")
        state = ("disabled" if held == "disabled" else
                 "parked" if held and "parked" in str(held) else
                 "paused" if held and ("paused" in str(held) or "snoozed" in str(held)) else
                 "held" if held else "scheduled")
        upcoming.append({"system": it.get("system"), "name": it.get("name") or "",
                         "profile": it.get("id") if it.get("kind") == "profile" else None,
                         "source": "Profile" if it.get("kind") == "profile" else "Scheduler page",
                         "schedule": it.get("schedule"), "next_run_at": it.get("next_run_at"),
                         "reason": held or "on schedule", "state": state,
                         "last_run": it.get("last_run")})

    systems = sorted(set(visible) | ({r.get("system") for r in runs if r.get("system")}
                                     if not scoped else set()))
    return {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "days": days,
            "system": system, "paused": nxt.get("paused"), "snoozed": nxt.get("snoozed"),
            "manual_hold": nxt.get("manual_hold"), "recipients_hidden": not admin,
            "delivery_timeout_s": outbox.timeout_seconds(), "systems": systems,
            "upcoming": upcoming, "runs": runs, "emails": emails[:1000],
            **_display_tz_info()}


def _display_tz_info() -> dict:
    """How pages turn the stored VM-local stamps into IST (utils/display_time.py)."""
    from utils.display_time import display_zone
    z = display_zone()
    probe = datetime.now()
    return {"server_utc_offset_min": int(probe.astimezone().utcoffset().total_seconds() // 60),
            "display_utc_offset_min": int(z.utcoffset(None).total_seconds() // 60),
            "display_tz_name": z.tzname(None)}


@app.get("/api/monitoring-log")
def get_monitoring_log(days: int = 7, system: str = "", limit: int = 200, result: str = ""):
    """Which monitoring ran, the mails sent for it (to whom / why not), and the next runs."""
    return _monitoring_log(days=days, system=system, limit=limit, result=result)


@app.get("/api/monitoring-log.csv")
def get_monitoring_log_csv(days: int = 7, system: str = "", limit: int = 1000, result: str = ""):
    from fastapi.responses import Response
    from core import monitoring_log as ml
    data = _monitoring_log(days=days, system=system, limit=limit, result=result)
    return Response(content=ml.to_csv(data["runs"]), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="monitoring_log.csv"'})


@app.get("/monitoring-log")
def monitoring_log_page():
    return _page("monitoring_log.html")


def _run_single_system(system_config: dict):
    """Run one system without allowing its failure to poison the scheduler."""
    import main as _main
    from main import run_pipeline_for_system
    name = system_config["name"]
    _run_state["current_system"] = name
    _run_state["started_at"] = datetime.now()          # elapsed time per system (also in "run all")
    try:
        from core import heartbeat as _hb
        _hb.progress(None, 0, 0)
    except Exception:  # noqa: BLE001
        pass
    # The Stop button reaches into the sweep: no more T-codes, no second
    # attempt, no sheet and no report retry (main._stop_requested).
    _main.STOP_CHECK = lambda: bool(_run_state.get("stop_requested"))
    _main.RCA_BLOCK = _rca_auto_block          # the sweep's own RCA obeys the same holds
    meta = system_config.get("_run_meta") if isinstance(system_config.get("_run_meta"), dict) else {}
    started_at = datetime.now()
    try:
        ok = bool(run_pipeline_for_system(system_config))
        _record_run_history(system_config, meta, started_at, ok)
        stopped = not ok and bool(_run_state.get("stop_requested"))
        if stopped:
            try:
                from core import heartbeat as _hb
                p = _hb.progress_snapshot()
            except Exception:  # noqa: BLE001
                p = {}
            _run_state["last_stop"] = {
                "system": name, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "requested_at": _run_state.get("stop_requested_at"),
                "after_tcode": p.get("tcode"), "done": p.get("index") or 0, "total": p.get("total") or 0}
        _append_run_history({
            "system": name,
            "event": "Sweep completed" if ok else "Stopped from the dashboard" if stopped else "Sweep failed",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ok" if ok else "stopped" if stopped else "error",
        })
        return ok
    except Exception as e:
        log.error("Pipeline error for %s: %s", name, type(e).__name__)
        _record_run_history(system_config, meta, started_at, False, e)
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


def _record_run_history(system_config: dict, meta: dict, started_at, ok: bool, error=None) -> None:
    """One row per finished run in the run history. Never raises."""
    try:
        from core import run_history
        name = system_config.get("name", "")
        ctx = system_config.get("_run_ctx") if isinstance(system_config.get("_run_ctx"), dict) else {}
        if ctx.get("result") in (None, "failed") and not ok and _run_state.get("stop_requested"):
            ctx["result"] = "stopped-by-user"
            system_config["_run_ctx"] = ctx
        nxt = None
        try:
            nxt = (next_runs().get("systems") or {}).get(name)
        except Exception:  # noqa: BLE001
            nxt = None
        run_history.record_run(system_config, trigger=meta.get("trigger") or "manual-system",
                               started_at=started_at, ok=ok, error=error,
                               planned_at=meta.get("planned_at"),
                               profile_id=meta.get("profile_id"),
                               next_run_at=(nxt or {}).get("at"),
                               next_run_reason="schedule" if nxt else None)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"run history not written: {exc}")

# Milestone 8.2: per-system failures are isolated; remaining systems continue.


def _run_all_systems_background():
    with _run_lock:
        if _run_state["running"]:
            _record_skipped_run("ALL SYSTEMS")
            return
        _run_state["running"] = True
        _run_state["error"] = None
        _run_state["cancel_requested"] = False
        _run_state["stopped_after"] = None
        _run_state["stop_requested"] = False

    try:
        _set_manual_report_run(True)
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

            if isinstance(sysconf, dict):
                sysconf.setdefault("_run_meta", {"trigger": "manual-all"})
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
        _end_manual_hold()


# Systems someone pressed Stop on: no automatic RCA and no report retry on
# them for a while (IBO_STOP_HOLD_MINUTES, default 240). A manual "Monitor
# this system" ends the hold; the daily schedule runs as usual.
_stop_hold: dict = {}
_rca_cancel = {"on": False}


def _stop_hold_minutes() -> int:
    try:
        return max(0, int(os.getenv("IBO_STOP_HOLD_MINUTES", "240") or 240))
    except ValueError:
        return 240


def _stop_hold_reason(system: str) -> str | None:
    until = _stop_hold.get(system)
    if not until:
        return None
    if datetime.now() >= until:
        _stop_hold.pop(system, None)
        return None
    return f"stopped from the dashboard (no automatic run until {until:%d.%m %H:%M})"


def _hold_until_next_slot(system: str) -> None:
    """After a Stop: hold automatic runs on `system` until its next planned slot."""
    _stop_hold.pop(system, None)                     # so next_runs() sees the plain next slot
    until = None
    try:
        nxt = (next_runs().get("systems") or {}).get(system)
        if nxt:
            until = datetime.strptime(nxt["at"], "%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        until = None
    if until is None or until <= datetime.now():
        until = datetime.now() + timedelta(minutes=_stop_hold_minutes() or 240)
    _stop_hold[system] = until
    _save_stop_holds()


def _save_stop_holds() -> None:
    """Keep holds across a dashboard restart (the watcher restarts it)."""
    try:
        import json as _json
        from core import ops_db
        data = {k: v.isoformat() for k, v in list(_stop_hold.items()) if v > datetime.now()}
        ops_db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                       "SET value=excluded.value", ("stop_holds", _json.dumps(data)))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"stop holds not saved: {exc}")


def _load_stop_holds() -> None:
    try:
        import json as _json
        from core import ops_db
        row = ops_db.one("SELECT value FROM meta WHERE key=?", ("stop_holds",))
        for k, v in (_json.loads(row["value"]) if row and row.get("value") else {}).items():
            until = datetime.fromisoformat(v)
            if until > datetime.now():
                _stop_hold[k] = until
    except Exception as exc:  # noqa: BLE001
        log.debug(f"stop holds not loaded: {exc}")


def _run_progress() -> dict:
    """What the wall shows while a sweep runs, and the last Stop."""
    running = bool(_run_state.get("running"))
    prog = None
    if running:
        try:
            from core import heartbeat as _hb
            prog = _hb.progress_snapshot()
        except Exception:  # noqa: BLE001
            prog = None
    st = _run_state.get("started_at")
    now = datetime.now()
    return {
        "stop_requested": running and bool(_run_state.get("stop_requested")),
        "stop_requested_at": _run_state.get("stop_requested_at") if running else None,
        "progress": prog,
        "elapsed_s": int((now - st).total_seconds()) if running and st else None,
        "last_stop": _run_state.get("last_stop"),
        "stop_hold": {k: v.strftime("%d.%m %H:%M") for k, v in list(_stop_hold.items()) if v > now},
    }


@app.post("/api/run-stop", dependencies=[Depends(require_token)])
def stop_run():
    """
    Stop the sweep or RCA in progress -- and the automatic follow-ups on that
    system (performance RCA, report retries) for IBO_STOP_HOLD_MINUTES.

    It no longer pauses automatic monitoring of every system: that silently
    skipped the next day's scheduled sheets of all systems (13 Oct). To pause
    everything, use Pause or Snooze on the Scheduler page.

    The system in flight is allowed to finish its current screen. Aborting it
    mid-way would leave SAP Logon open and a half-written snapshot behind.
    """
    was_running = bool(_run_state["running"])
    current = _run_state.get("current_system")
    rca_system = _rca_state.get("system") if _rca_state.get("running") else None
    if _user_is_scoped():
        allowed = {s.get("name") for s in get_systems()}
        foreign = [x for x in (current, rca_system) if x and x not in allowed]
        if foreign or (was_running and not current):
            raise HTTPException(status_code=403, detail=(
                "The run in progress belongs to a system outside your customers; only its owner "
                "or an administrator can stop it."))

    _run_state["cancel_requested"] = True
    _run_state["stop_requested"] = True
    if was_running and not _run_state.get("stop_requested_at"):
        _run_state["stop_requested_at"] = datetime.now().strftime("%H:%M:%S")
    if rca_system or _run_state.get("profile_id") == "rca":
        _rca_cancel["on"] = True
    held = sorted({x for x in (current, rca_system) if x})
    minutes = _stop_hold_minutes()
    for name in held:
        if minutes:
            _stop_hold[name] = datetime.now() + timedelta(minutes=minutes)
            _save_stop_holds()
        try:
            from core import report_gate as _rg
            _rg.clear(name)                       # no report retry after a Stop
        except Exception:
            pass
        try:
            from core import timeline as _tl
            _tl.add(name, "sweep", "Stopped from the dashboard",
                    "no automatic run, RCA or report retry on it until its next scheduled run",
                    severity="info")
        except Exception:
            pass
    log.info(f"Stop requested from the dashboard. "
             f"{'Stopping ' + ', '.join(held) + '.' if held else 'Nothing was running.'} "
             f"Automatic RCA and report retries on it held for {minutes} min; "
             f"other systems' schedules carry on.")
    until = (datetime.now() + timedelta(minutes=minutes)).strftime("%H:%M")
    return {
        "stopping": was_running,
        "current_system": current,
        "held": held,
        "scheduler_paused": False,
        "note": (f"Stopping {', '.join(held)} after the current screen. Nothing starts on it again "
                 f"automatically before its next scheduled run; other systems carry on."
                 if held else
                 "Nothing was running. To pause all automatic monitoring use Pause on the Scheduler page."),
    }


@app.post("/api/run-now", dependencies=[Depends(require_token), Depends(require_collector_role)])
def run_now():
    _claim_manual_run("all systems")
    _begin_manual_hold()
    thread = threading.Thread(target=_claimed(_run_all_systems_background), daemon=True)
    thread.start()
    return {"started": True, "scope": "all"}


def _begin_manual_hold() -> None:
    """
    A person started a run: automatic monitoring is HELD while it runs.

    The hold is its own flag (manual_pause), not `enabled`: it used to switch
    automatic monitoring off and rely on the end of the run to switch it
    back, which went wrong (a restart re-enabled it with overdue profiles
    firing; a Run-profile run did not hold at all). The hold now ends by
    itself in _end_manual_hold() when the manual run finishes.
    """
    _scheduler_state["manual_pause"] = True
    _scheduler_state["manual_since"] = datetime.now()


def _end_manual_hold() -> None:
    """The manual run is over: what fell due meanwhile is recorded as skipped
    and waits for its next slot; automatic monitoring carries on."""
    if not _scheduler_state.get("manual_pause"):
        return
    try:
        _skip_due_after_manual_run()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"Could not re-time due schedules after a manual run: {exc}")
    _scheduler_state["manual_pause"] = False
    _scheduler_state["manual_since"] = None
    log.info("Manual run finished -- automatic monitoring hold released.")


def _set_manual_report_run(on: bool) -> None:
    try:
        from core import report_gate as _rg
        _rg.set_manual_run(on)
    except Exception:
        pass


def _active_run_path() -> str:
    d = os.getenv("IBO_RUNTIME_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                     "runtime")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "active_run.json")


_stamps_before: dict = {}        # profile id -> its slot stamps before the scheduler marked this run


def _active_run_mark(system_name, profile_id=None, trigger=None, planned_at=None) -> None:
    """runtime/active_run.json while a sweep runs; gone when it ends. Never raises."""
    try:
        path = _active_run_path()
        if system_name is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"system": system_name, "profile_id": profile_id, "trigger": trigger,
                       "planned_at": planned_at.isoformat(timespec="seconds") if isinstance(planned_at, datetime)
                       else planned_at, "started_at": datetime.now().isoformat(timespec="seconds"),
                       "pid": os.getpid(), "stamps_before": _stamps_before.pop(profile_id, None)
                       if profile_id else None}, fh)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"active-run marker not written: {exc}")


def _pid_alive(pid: int) -> bool:
    """Is process `pid` still running? Never sends a signal (os.kill on Windows would end it)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, int(pid))          # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() == 5               # access denied: it exists
            try:
                code = wintypes.DWORD()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259             # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        os.kill(int(pid), 0)                                      # POSIX: signal 0 only checks
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _recover_interrupted_run() -> None:
    """
    At start: a sweep that was running when the dashboard stopped (restart,
    crash, VM reboot) never wrote its run-history row. Record it as failed,
    so the Monitoring log shows the gap instead of nothing.
    """
    try:
        path = _active_run_path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            a = json.load(fh)
        pid = int(a.get("pid") or 0)
        if pid == os.getpid():
            return
        if _pid_alive(pid):
            # Another dashboard process is running this sweep right now. Marking
            # it failed and putting its slot back made the same profile run
            # 2-3 times in one hour (28.10: prd-quick 14:22, 14:26, 14:29).
            log.warning(f"Run marker belongs to live process {pid} (another dashboard) -- left alone.")
            return
        os.remove(path)
        started = str(a.get("started_at") or "").replace("T", " ")
        _history_decision(a.get("system") or "?", "failed", a.get("trigger") or "schedule",
                          a.get("profile_id"), a.get("planned_at"),
                          f"the dashboard stopped during this run (started {started}: restart, crash or "
                          f"VM reboot) -- it did not finish and sent no report")
        log.warning(f"Run of {a.get('system')} started {started} was interrupted by a dashboard stop; "
                    f"recorded as failed in the run history.")
        # Its slot is due again: it runs as soon as the scheduler starts (within
        # the slot's grace window), instead of waiting for the next slot.
        if a.get("profile_id") and isinstance(a.get("stamps_before"), list):
            try:
                from core.profiles import restore_stamps
                restore_stamps(a["profile_id"], tuple(a["stamps_before"]))
                log.info(f"Profile '{a['profile_id']}': the interrupted slot will run again now.")
            except Exception as exc:  # noqa: BLE001
                log.debug(f"interrupted slot not restored: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"interrupted-run check skipped: {exc}")


def _run_one_system_background(system_name: str, profile_id: str | None = None,
                               resume_scheduler_after: bool = False, narrow_to_profile: str | None = None,
                               trigger: str | None = None, planned_at=None) -> bool:
    """
    Runs the full pipeline for a single system, then clears run state.
    Returns False when it could not start (another run holds the desktop --
    recorded as skipped in the run history).

    `profile_id` records which profile asked for this run, so the UI can say
    why a sweep is happening. A run that appears with no explanation is the
    thing this change was made to remove. `trigger` / `planned_at` go to the
    run history (derived from profile_id when not given).

    A MANUAL run holds automatic monitoring while it runs (manual_pause,
    set by the endpoint) and the hold ends by itself here; what fell due
    meanwhile is recorded as skipped and waits for its next slot, so a
    manual sweep never chains straight into a scheduled one.
    `resume_scheduler_after=True` also switches automatic monitoring on.
    """
    trigger = trigger or _trigger_for(profile_id, narrow_to_profile)
    with _run_lock:
        if _run_state["running"]:
            _record_skipped_run(system_name, profile_id, trigger=trigger, planned_at=planned_at)
            return False
        _run_state["running"] = True
        _run_state["error"] = None
        _run_state["cancel_requested"] = False
        _run_state["stop_requested"] = False
        _run_state["stop_requested_at"] = None
        _run_state["started_at"] = datetime.now()
        _run_state["current_system"] = system_name
        _run_state["profile_id"] = profile_id
    _active_run_mark(system_name, profile_id, trigger, planned_at)
    try:
        from core import heartbeat as _hb
        _hb.progress(None, 0, 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = next((s for s in get_systems() if s.get("name") == system_name), None)
        if cfg is None:
            _run_state["error"] = f"Unknown system: {system_name}"
            return True
        _set_manual_report_run(trigger in ("manual-system", "manual-profile", "new-system"))
        # A profile that names its own T-codes narrows this run's set
        # (main._tasks_this_run); a copy, so nothing sticks to the system.
        cfg = dict(cfg)
        if narrow_to_profile or profile_id:
            cfg["_profile_id"] = narrow_to_profile or profile_id
        # Plain values only: the system config also travels to the RFC
        # worker process and into JSON in places.
        from core.schedules import iso_stamp as _iso_stamp
        cfg["_run_meta"] = {"trigger": trigger,
                            "planned_at": _iso_stamp(planned_at) if isinstance(planned_at, datetime)
                            else planned_at,
                            "profile_id": narrow_to_profile or (
                                profile_id if trigger in ("schedule", "catch-up") and
                                profile_id != SCHEDULE_RUN_LABEL else None)}
        _run_single_system(cfg)
    except Exception as exc:
        log.error("Single-system run failed for %s: %s", system_name, exc)
        _run_state["error"] = str(exc)
    finally:
        stopped_by_user = bool(_run_state.get("stop_requested"))
        with _run_lock:
            _run_state["running"] = False
            _run_state["current_system"] = None
            _run_state["profile_id"] = None
        _active_run_mark(None)
        if stopped_by_user:
            # Stop means stop: what fell due while it ran moves to its next
            # slot, and nothing (profile, schedule, retry, RCA) starts on this
            # system again before that slot.
            _skip_due_after_manual_run(only=system_name)   # other systems keep their slots
            _hold_until_next_slot(system_name)
        # End of a manual run: the hold ends by itself (what fell due
        # meanwhile is recorded as skipped and waits for its next slot).
        if trigger in ("manual-system", "manual-profile", "new-system"):
            _end_manual_hold()
        if resume_scheduler_after:
            _scheduler_state["enabled"] = True
    return True


@app.post("/api/remote-run-now/{system_name}", dependencies=[Depends(require_token)])
def remote_run_now(system_name: str):
    """
    "Monitor this system" on a card supplied by another collector.

    The system is not configured here, so nothing can run here. The click is
    forwarded to the collector that supplies it (config/remote_sources.yaml),
    which runs the sweep and whose answer -- started, busy, or refused -- is
    passed back unchanged. This dashboard's own run state is not touched.
    """
    from dashboard import remote_sources
    status, body = remote_sources.start_remote_run(system_name)
    if status >= 300:
        raise HTTPException(status_code=status, detail=body.get("detail") or f"collector answered HTTP {status}")
    return body


@app.get("/api/remote-run-status/{system_name}")
def remote_run_status(system_name: str):
    """Run state of the collector that supplies `system_name` (see above)."""
    from dashboard import remote_sources
    return remote_sources.remote_run_status(system_name)


@app.post("/api/run-now/{system_name}", dependencies=[Depends(require_token), Depends(require_collector_role)])
def run_now_single(system_name: str):
    """
    Monitor ONE system.

    A full sweep drives SAP GUI for every configured system and takes
    minutes; when an operator wants to re-check the system they are actually
    looking at, running all of them is the wrong tool. It also means a
    problem on one system no longer forces a wait on three others.
    """
    if not any(s.get("name") == system_name for s in get_systems()):
        raise HTTPException(status_code=404, detail=f"Unknown system: {system_name}")
    _claim_manual_run(system_name)

    # A manual run must not silently hand off to a scheduled one: the moment
    # it freed _run_state, the scheduler's next 20 s pass could find a profile
    # due and launch it (a manual CARFOUR run was followed by an unrequested
    # PRD sweep). Automatic monitoring is HELD for the duration -- a separate
    # flag, so Pause/Resume keep their meaning -- and the hold ends by itself
    # when the run ends; what fell due meanwhile waits for its next slot.
    _begin_manual_hold()
    _stop_hold.pop(system_name, None)          # a person asked for this system again
    _save_stop_holds()

    thread = threading.Thread(target=_claimed(_run_one_system_background),
                              args=(system_name,),
                              kwargs={"resume_scheduler_after": False},
                              daemon=True)
    thread.start()
    return {"started": True, "scope": system_name,
            "note": "Automatic monitoring is paused during this manual run."}


@app.get("/api/run-status")
def run_status():
    """
    This process's run state, merged with the cross-process sweep lock: in
    the split deployment the sweep happens in the collector process, and
    the web process would otherwise always say "not running".
    """
    out = {**_run_state, **_scheduler_state}
    out["config_error"] = _config_error()
    try:
        from core.run_lock import owner
        who = owner()
        if who and not out.get("running"):
            out["running"] = True
            out["current_system"] = who.get("system")
            out["run_owner"] = who
        from core import store
        hb = {h["service"]: h for h in store.heartbeats()}
        if "collector" in hb:
            out["collector"] = hb["collector"]
            out["enabled"] = out.get("enabled") or (hb["collector"].get("age_seconds") or 9999) < 300
    except Exception:
        pass
    return out


def _scheduler_loop():
    """
    Per-system monitoring schedule.

    Each system has its own cadence (see core/schedules.py). The loop wakes
    every 20 seconds, asks which systems are due, and runs them one at a
    time -- never concurrently, because a sweep drives SAP GUI and two at
    once would fight over the same desktop.

    Startup behaviour:
        a slot still inside the grace window (IBO_SCHEDULE_GRACE_MINUTES,
        default 60) runs once after a restart (trigger "catch-up"); older
        slots are recorded as missed. MONITOR_ON_STARTUP is accepted for
        compatibility and does not change this.
        SCHEDULER_ENABLED=false    never run automatically
    """
    def _flag(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        return default if raw is None else str(raw).strip().lower() in ("1", "true", "yes", "on")

    if not _flag("SCHEDULER_ENABLED", True):
        log.info("Scheduler disabled (SCHEDULER_ENABLED=false). Manual runs only.")
        _scheduler_state["enabled"] = False
        return

    # One scheduler per machine (core/instance_lock.py). A second dashboard
    # process waits here on standby instead of running every schedule again.
    from core import instance_lock
    told = False
    while not instance_lock.acquire("scheduler"):
        if not told:
            log.warning("Scheduler on standby: another IBOPS process on this machine already runs "
                        "the scheduler. This one takes over only if that one stops.")
            told = True
        _scheduler_state["standby"] = True
        time_module.sleep(60)
    _scheduler_state["standby"] = False
    # Only the scheduler holder looks at a left-over run marker, and only now:
    # a standby process never touches the live run of the process it waits on.
    _recover_interrupted_run()
    if told:
        log.info("Scheduler: the other process stopped; this process now runs the scheduler.")

    # Slots that passed while the dashboard was not running: one still inside
    # the grace window runs once as a "catch-up" (the daily 07:00 run is not
    # lost to a restart at 07:10); older ones are recorded as missed and the
    # schedule waits for its next slot. MONITOR_ON_STARTUP no longer changes
    # this: .env.example shipped it as "false", and honouring that would lose
    # exactly those runs. Startup problems are logged; the loop runs anyway.
    if os.getenv("MONITOR_ON_STARTUP") is not None:
        log.info("Scheduler: MONITOR_ON_STARTUP is set but no longer used -- a slot within the "
                 "grace window always runs once after a restart; older slots are recorded missed.")
    try:
        _apply_startup_policy(True)
    except Exception as exc:  # noqa: BLE001 -- the scheduler must start regardless
        log.error(f"Scheduler: startup catch-up check failed: {type(exc).__name__}: {exc}")
        _scheduler_state["loop_error"] = f"startup: {type(exc).__name__}: {exc}"[:300]

    try:
        from core.profiles import load_profiles
        enabled = [p for p in load_profiles() if p["schedule"].get("enabled")]
        from core.schedules import load_schedules, describe as _describe
        stored = load_schedules()
        scheduled = [(s.get("name"), stored[s.get("name")]) for s in get_systems()
                     if (stored.get(s.get("name")) or {}).get("enabled")]
        if enabled:
            log.info("Scheduler: profiles: " + "; ".join(
                f"{p['id']} ({p['system']} -> {','.join(p['deliver']) or 'no destination'})"
                for p in enabled))
        if scheduled:
            log.info("Scheduler: Scheduler-page schedules: " + "; ".join(
                f"{n} ({_describe(c)})" for n, c in scheduled))
        if not enabled and not scheduled:
            # Said explicitly. A quiet scheduler and a broken one look
            # identical in a log that says nothing.
            log.warning("Scheduler: no enabled profiles and no saved schedules -- "
                        "nothing will run automatically.")
    except Exception as exc:
        log.error(f"Scheduler: could not read profiles or schedules: {exc}")

    while True:
        _scheduler_pass()

        # Once a day: delete evidence and reports past their retention
        # window (core/retention.py). Cheap, and this loop is already the
        # one place that runs regardless of what the UI is doing.
        try:
            _retention_tick()
        except Exception as exc:
            log.warning(f"retention pass failed: {exc}")

        # Short sleep so a pause from the UI takes effect within seconds
        # rather than at the end of a multi-hour interval.
        time_module.sleep(20)


_retry_once: dict = {}


def _stop_pending() -> bool:
    """runtime/stop_pending (stop_dashboard.bat): start no new sweep; ignored after 30 min."""
    try:
        path = os.path.join(os.path.dirname(_active_run_path()), "stop_pending")
        return os.path.exists(path) and time_module.time() - os.path.getmtime(path) < 1800
    except Exception:  # noqa: BLE001
        return False


def _scheduler_pass() -> None:
    """One scheduler pass (every 20 s): snooze expiry, manual-hold check,
    then at most ONE run started -- a due profile, a due report retry or a
    due Scheduler-page schedule. Never raises (the loop must never die)."""
    try:
        snooze = _scheduler_state.get("snooze_until")
        snoozed = bool(snooze and datetime.now() < snooze)
        if snooze and not snoozed:
            _scheduler_state["snooze_until"] = None
            _scheduler_state["paused_info"] = None
            log.info("Snooze expired -- automatic monitoring resumed.")

        # Report retries (core/report_gate.py) are only promised while
        # automatic monitoring is on (or paused by a manual run that
        # resumes it by itself); this tells the gate which it is.
        try:
            from core import report_gate as _rg_alive
            _on = _scheduler_state["enabled"] and not snoozed
            _rg_alive.mark_scheduler_alive(paused=not _on)
        except Exception:
            pass
        # A manual hold whose run never started (refused, crashed before
        # taking the run lock) must not hold automatic monitoring forever.
        _since = _scheduler_state.get("manual_since")
        if (_scheduler_state.get("manual_pause") and not _run_state["running"]
                and (_since is None or datetime.now() - _since > timedelta(minutes=2))):
            _end_manual_hold()
        if not (_scheduler_state["enabled"] and not snoozed) and not _scheduler_state.get("manual_pause"):
            _notice_paused_slots(snoozed)
        # A sweep, an RCA or a manual run holding SAP GUI: due slots wait for
        # it, and the time spent waiting does not count against their grace
        # window (core/schedules.busy_window) -- they run as soon as it ends.
        if _run_state["running"] or _scheduler_state.get("manual_pause"):
            if not _scheduler_state.get("busy_since"):
                _scheduler_state["busy_since"] = datetime.now()
        if _stop_pending():
            return                         # stop_dashboard.bat is waiting for the running sweep
        if (_scheduler_state["enabled"] and not snoozed and not _run_state["running"]
                and not _scheduler_state.get("manual_pause")):
            # TWO SOURCES OF SCHEDULED RUNS, both chosen by a person:
            #   1. profiles on the Profiles page (checked first);
            #   2. per-system schedules SAVED on the Scheduler page
            #      (a full sweep, as "Monitor now" on the card).
            #
            # There is still no global interval and no default: a system
            # with no saved schedule and no enabled profile never runs on
            # its own. On a production system a sweep drives SAP GUI for
            # several minutes and emails a report, so it must happen
            # because someone asked for it, not because a default elapsed.
            from core.profiles import (due_profiles, mark_run, set_handled,
                                       status as profile_status)

            from core.schedules import busy_window as _busy_window
            _late = _busy_window(_scheduler_state.get("busy_since"))
            due = due_profiles(window=_late)
            # Maintenance windows and parked systems (core/alert_control.py):
            # their scheduled sweeps are skipped; the skip is recorded on
            # the timeline and in the run history, and the slot is handled.
            while due:
                _block = _sweep_block_reason(due[0]["system"]) or _stop_hold_reason(due[0]["system"])
                if not _block:
                    break
                _skipped = due.pop(0)
                set_handled(_skipped["id"], _skipped.get("planned_at"))
                log.info(f"Scheduler: profile '{_skipped['id']}' skipped -- "
                         f"{_skipped['system']} is {_block}.")
                _history_decision(_skipped["system"],
                                  "parked" if str(_block).startswith("parked") else "skipped",
                                  "schedule", _skipped["id"], _skipped.get("planned_at"), _block)
                try:
                    from core import timeline as _tl
                    _tl.add(_skipped["system"], "sweep", "Scheduled sweep skipped",
                            f"{_block} (profile {_skipped['id']})", severity="info")
                except Exception:
                    pass
            if due:
                # Oldest slot first: an hourly profile must not keep a daily
                # one (whose slot has been waiting longer) behind it.
                due.sort(key=lambda p: p.get("planned_at") or datetime.max)
                profile = due[0]
                slot = profile.get("planned_at")
                log.info(f"Scheduler: profile '{profile['id']}' is due "
                         f"({profile['system']}, slot {slot:%H:%M}, "
                         f"{len(profile['resolved_tcodes'])} T-code(s), "
                         f"-> {profile['deliver'] or 'no destination'}).")
                _scheduler_state["last_cycle_start"] = \
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _scheduler_state["last_profile"] = profile["id"]
                # The PLANNED slot is stamped, before the run: a sweep
                # that crashes is not due again, and however long the
                # sweep takes the next slot stays where the grid puts it.
                from core.profiles import stamps as _stamps, restore_stamps as _restore
                _before = _stamps(profile["id"])
                _stamps_before[profile["id"]] = list(_before)
                mark_run(profile["id"], slot)
                # The scheduler's OWN sweep counts as busy time too: a second
                # slot waiting behind a 70-minute sweep is not "missed"
                # (review 28.10.2026; no pass runs while this thread sweeps).
                if not _scheduler_state.get("busy_since"):
                    _scheduler_state["busy_since"] = datetime.now()
                started = _run_one_system_background(profile["system"], profile_id=profile["id"],
                                                     trigger=_trigger_once("profile", profile["id"]),
                                                     planned_at=slot)
                if started is False and _retry_once.setdefault(f"{profile['id']}|{slot}", 0) < 1:
                    # Another run took SAP GUI in the moment between the
                    # check and the start (manual run, RCA): the slot is not
                    # lost -- it is due again once that run ends (once).
                    _retry_once[f"{profile['id']}|{slot}"] += 1
                    if len(_retry_once) > 500:
                        _retry_once.clear()
                    _restore(profile["id"], _before)
                # One profile per pass, then re-evaluate. A long sweep
                # would otherwise let several profiles pile up behind it.
            elif _run_due_report_retry():
                # A monitoring run whose report was held back (incomplete)
                # is tried again (core/report_gate.py). One per pass.
                pass
            elif _run_due_system_schedule(_late):
                # A Scheduler-page schedule ran (one per pass, like
                # profiles). Re-evaluate on the next pass.
                pass
            else:
                _scheduler_state["busy_since"] = None      # idle and nothing due
                upcoming = [p["next_run"].replace("T", " ")
                            for p in profile_status() if p.get("next_run")]
                upcoming += _upcoming_system_schedules()
                _scheduler_state["next_run_at"] = (
                    min(upcoming) if upcoming else None)
        _scheduler_state["loop_error"] = None
    except Exception as exc:
        # The scheduler must never die: a bad schedule entry would
        # otherwise stop all automatic monitoring until someone noticed.
        log.error(f"Scheduler pass failed: {type(exc).__name__}: {exc}")
        _scheduler_state["loop_error"] = f"{type(exc).__name__}: {exc}"[:300]


SCHEDULE_RUN_LABEL = "Scheduler page"


REPORT_RETRY_LABEL = "Retry: report was incomplete"


def _run_due_report_retry() -> bool:
    """Run ONE system whose monitoring report was held back and is due to be tried again."""
    if _run_state.get("running"):
        return False                       # a sweep is running: try on a later pass
    try:
        from core import report_gate as _rg
        due = _rg.due_retries(system_configs={s.get("name"): s for s in get_systems()})
        if not due:
            return False
        name = due[0]
        known = {s.get("name") for s in get_systems()}
        if name not in known:
            _rg.clear(name)
            return False
        block = _sweep_block_reason(name) or _stop_hold_reason(name)
        if block:
            _rg.postpone(name, 15)
            log.info(f"Scheduler: report retry of {name} postponed -- {name} is {block}.")
            return False
        # Marked before the run: a run that crashes before it reaches the report
        # step is not started again on every pass, and counts as a failed try.
        _rg.mark_started(name)
    except Exception as exc:  # noqa: BLE001 -- never stop the scheduler over a retry
        log.warning(f"Scheduler: report retry skipped: {type(exc).__name__}: {exc}")
        return False
    # The retry runs as the profile whose run was held (its delivery,
    # recipients and T-codes), not as a bare system run (review 28.10.2026).
    try:
        pid = _rg.retry_profile(name)
    except Exception:  # noqa: BLE001
        pid = None
    log.info(f"Scheduler: {name} -- {REPORT_RETRY_LABEL}" + (f" (profile {pid})." if pid else "."))
    _scheduler_state["last_profile"] = REPORT_RETRY_LABEL
    if not _scheduler_state.get("busy_since"):
        _scheduler_state["busy_since"] = datetime.now()
    if not _run_one_system_background(name, profile_id=pid or REPORT_RETRY_LABEL, trigger="retry"):
        try:
            _rg.unmark_started(name)       # another run held SAP GUI: not a used try
        except Exception:  # noqa: BLE001
            pass
    return True


def _run_due_system_schedule(window=None) -> bool:
    """
    Run at most ONE system whose Scheduler-page schedule is due.

    Returns True when a sweep was started (or a due one was skipped because
    the system is in maintenance / parked), so the loop re-evaluates on its
    next pass instead of starting a second one now.
    """
    from core import schedules as _sch

    def _missed(system: str, slot) -> None:
        _grace_min = int(_sch.grace().total_seconds() // 60)
        try:
            from core import timeline as _tl
            _tl.add(system, "sweep", "Scheduled sweep missed",
                    f"due {slot:%H:%M}, could not start within {_grace_min} min "
                    f"({SCHEDULE_RUN_LABEL}); it waits for its next slot", severity="warning")
        except Exception:
            pass
        _history_decision(system, "missed", "schedule", None, slot,
                          f"could not start within {_grace_min} min of its slot")
        _schedule_notice(system, slot,
                         f"it could not start within {_grace_min} min of its time (another sweep was "
                         f"running long, or the dashboard was not running at that time)",
                         "Press 'Monitor this system' to run it now; otherwise it runs at its next slot.")

    due = _sch.due_systems_with_slots([s.get("name") for s in get_systems()], on_missed=_missed,
                                      window=window)
    if not due:
        return False
    name, slot = due[0]
    block = _sweep_block_reason(name) or _stop_hold_reason(name)
    if block:
        _sch.set_handled(name, slot)
        log.info(f"Scheduler: scheduled sweep of {name} skipped -- {name} is {block}.")
        _history_decision(name, "parked" if str(block).startswith("parked") else "skipped",
                          "schedule", None, slot, block)
        if block.startswith("parked"):
            _schedule_notice(name, datetime.now().replace(second=0, microsecond=0),
                             f"the system's sweeps are {block}",
                             "Fix the cause (often the SAP user's password), clear the parked flag "
                             "on the Live wall, then press 'Monitor this system'.", once_per_day=True,
                             kind="parked")
        try:
            from core import timeline as _tl
            _tl.add(name, "sweep", "Scheduled sweep skipped",
                    f"{block} ({SCHEDULE_RUN_LABEL})", severity="info")
        except Exception:
            pass
        return True

    cfg = _sch.load_schedules().get(name)
    log.info(f"Scheduler: {name} is due ({_sch.describe(cfg)}, {SCHEDULE_RUN_LABEL}) "
             f"-- full sweep.")
    _scheduler_state["last_cycle_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _scheduler_state["last_profile"] = SCHEDULE_RUN_LABEL
    # The PLANNED slot is stamped before the run (a crashing sweep must not
    # loop, and the grid never drifts with how long a sweep took) -- the same
    # rule as for profiles.
    _sch.mark_run(name, slot)
    if not _scheduler_state.get("busy_since"):
        _scheduler_state["busy_since"] = datetime.now()
    _run_one_system_background(name, profile_id=SCHEDULE_RUN_LABEL,
                               trigger=_trigger_once("schedule", name), planned_at=slot)
    return True


def _schedule_notice(system: str, slot, reason: str, advice: str, once_per_day: bool = False,
                     kind: str = "slot") -> None:
    """Mail Basis once that a scheduled run did not happen (core/schedule_notice.py). Never raises."""
    try:
        from core import schedule_notice
        cfg = next((c for c in get_systems() if c.get("name") == system), None)
        if cfg:
            schedule_notice.not_run(cfg, slot, reason, advice, once_per_day=once_per_day, kind=kind)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"schedule notice skipped: {exc}")


def _notice_paused_slots(snoozed: bool) -> None:
    """Automatic monitoring is off: say so for each schedule that falls due meanwhile."""
    try:
        from core import schedule_notice
        if not schedule_notice.enabled():
            return
        how = _scheduler_state.get("paused_info") or (
            "snoozed" if snoozed else "paused (Pause or Stop pressed, or a manual run that did not hand back)")
        for name, slot in schedule_notice.due_while_paused([s.get("name") for s in get_systems()]):
            block = _sweep_block_reason(name)
            if block and not block.startswith("parked"):
                continue                      # maintenance / standby / VPN off: chosen by a person
            _schedule_notice(name, slot, f"automatic monitoring is {how}",
                             "Press Resume on the Scheduler page within the hour and it still runs today; "
                             "or press 'Monitor this system'.")
    except Exception as exc:  # noqa: BLE001 -- never stop the scheduler over this
        log.debug(f"paused-slot notice skipped: {exc}")


def _upcoming_system_schedules() -> list[str]:
    """Next run ("YYYY-MM-DD HH:MM:SS") of every saved, enabled system schedule."""
    from core import schedules as _sch
    stored = _sch.load_schedules()
    now = datetime.now()
    out = []
    for s in get_systems():
        name = s.get("name")
        nxt = _sch.planned_next(stored.get(name), _sch.handled_until(name), now)
        if nxt:
            out.append(nxt.strftime("%Y-%m-%d %H:%M:%S"))
    return out


def _skip_due_after_manual_run(only: str | None = None) -> None:
    """
    After a manual run, whatever fell due while it ran is NOT started at
    once: it is recorded as handled now, so it runs at its next slot. This
    keeps a manual check from chaining straight into a scheduled sweep.
    """
    now = datetime.now()
    why = "stopped from the dashboard" if only else "fell due during a manual run"
    try:
        from core.profiles import due_profiles, set_handled as profile_handled
        for p in due_profiles(now):
            if only and p.get("system") != only:
                continue
            profile_handled(p["id"], now)
            log.info(f"Scheduler: profile '{p['id']}' {why} -- moved to its next slot.")
            _history_decision(p["system"], "skipped", "schedule", p["id"], p.get("planned_at"), why)
    except Exception as exc:
        log.warning(f"Could not re-time due profiles after a manual run: {exc}")
    try:
        from core import schedules as _sch
        for name, slot in _sch.due_systems_with_slots([s.get("name") for s in get_systems()], now):
            if only and name != only:
                continue
            _sch.set_handled(name, now)
            log.info(f"Scheduler: {name} {why} -- moved to its next slot.")
            _history_decision(name, "skipped", "schedule", None, slot, why)
    except Exception as exc:
        log.warning(f"Could not re-time due schedules after a manual run: {exc}")


_catch_up: set = set()          # ("profile", id) / ("schedule", system) allowed to catch up once


def _trigger_once(kind: str, ident: str) -> str:
    """"catch-up" for the one run the startup policy allowed, else "schedule"."""
    if (kind, ident) in _catch_up:
        _catch_up.discard((kind, ident))
        return "catch-up"
    return "schedule"


def _apply_startup_policy(run_missed: bool = True, now: datetime | None = None) -> list:
    """
    Called once when the scheduler starts. Every slot that passed while the
    dashboard was not running:
      * older than the grace window  -> recorded as missed, never run;
      * within it (the default, run_missed=True) -> left due: the first pass
        runs it once (trigger "catch-up");
      * within it, run_missed=False (tests / callers only) -> recorded as
        missed, the schedule waits for its next slot.
    Returns [(kind, id, system, decision, slot)] for logging and tests.
    """
    from core import profiles as _pr
    from core import schedules as _sch
    now = now or datetime.now()
    out = []
    for profile, state, slot in _pr.startup_pending(now):
        if state == "due" and run_missed:
            _catch_up.add(("profile", profile["id"]))
            out.append(("profile", profile["id"], profile["system"], "catch-up", slot))
            continue
        _pr.set_handled(profile["id"], slot if state == "missed" else now)
        reason = ("dashboard was not running at the slot" if state == "missed" else
                  "dashboard started after the slot (catch-up disabled)")
        _history_decision(profile["system"], "missed", "schedule", profile["id"], slot, reason)
        out.append(("profile", profile["id"], profile["system"], "missed", slot))
    for name, state, slot in _sch.startup_pending([s.get("name") for s in get_systems()], now):
        if state == "due" and run_missed:
            _catch_up.add(("schedule", name))
            out.append(("schedule", SCHEDULE_RUN_LABEL, name, "catch-up", slot))
            continue
        _sch.set_handled(name, slot if state == "missed" else now)
        reason = ("dashboard was not running at the slot" if state == "missed" else
                  "dashboard started after the slot (catch-up disabled)")
        _history_decision(name, "missed", "schedule", None, slot, reason)
        out.append(("schedule", SCHEDULE_RUN_LABEL, name, "missed", slot))
    for kind, ident, system, decision, slot in out:
        log.info(f"Scheduler start: {kind} {ident} ({system}) slot {slot:%Y-%m-%d %H:%M} -> {decision}.")
    if not out:
        log.info("Scheduler start: no slot passed while the dashboard was not running.")
    return out


def _sweep_block_reason(system: str) -> str | None:
    try:
        from core.alert_control import sweep_block_reason
        return sweep_block_reason(system)
    except Exception as exc:  # noqa: BLE001 -- never stop the scheduler over this
        log.debug(f"sweep block check failed: {exc}")
        return None


_retention_last: dict = {"at": None}


def _retention_tick():
    last = _retention_last["at"]
    if last and (datetime.now() - last) < timedelta(hours=24):
        return
    _retention_last["at"] = datetime.now()
    from core.retention import run as _retention_run
    _retention_run()


def start_scheduler():
    if _running_under_pytest():
        log.info("Scheduler not started: running under pytest.")
        return
    if not sweeps_allowed():
        log.info("Scheduler not started: IBO_ROLE=ui (this process serves the wall only).")
        _scheduler_state["enabled"] = False
        return
    _load_stop_holds()                       # a Stop survives a dashboard restart
    # Load the sweep code now, not on the first "Monitor this system" click
    # (importing main and its report/AI modules took 3-10 s there).
    def _preload_main():
        try:
            import main  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            log.debug(f"sweep code preload skipped: {exc}")
    threading.Thread(target=_preload_main, name="preload-main", daemon=True).start()
    from core import supervisor
    supervisor.start("scheduler", _scheduler_loop)
    try:
        from core import vpn_keeper
        if vpn_keeper.configured():
            supervisor.start("vpn-keeper", vpn_keeper.loop)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"VPN keeper not started: {exc}")
    log.info("Scheduler started -- runs what the Profiles page and the saved "
             "Scheduler-page schedules ask for.")


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
    if "\x00" in path:
        raise HTTPException(status_code=404, detail="Not found")
    root = os.path.realpath(REPORTS_DIR)
    target = os.path.realpath(os.path.join(root, path))
    if not (target == root or target.startswith(root + os.sep)):
        raise HTTPException(status_code=403, detail="Path outside reports directory")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Not found")
    _check_report_scope(os.path.relpath(target, root))
    return FileResponse(target)


def _report_owner(rel_path: str, systems: list[dict]) -> str | None:
    """
    The system a file under reports/ belongs to: its folder
    (reports/<date>/<SYSTEM>/...), else the longest system name its file name
    starts with (older flat files: reports/<date>/<SYSTEM>_report.xlsx).
    None when it cannot be attributed.
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    by_safe = {}
    for cfg in systems:
        n = cfg.get("name")
        if n:
            by_safe[_safe_system_name(n).lower()] = n
            by_safe.setdefault(str(n).lower(), n)
    for seg in parts[:-1]:
        hit = by_safe.get(seg.lower())
        if hit:
            return hit
    fname = parts[-1].lower() if parts else ""
    best = None
    for key, n in by_safe.items():
        if fname.startswith(key) and (len(fname) == len(key) or not fname[len(key)].isalnum()):
            if best is None or len(key) > len(best[0]):
                best = (key, n)
    return best[1] if best else None


def _check_report_scope(rel_path: str) -> None:
    """
    A user limited to some customers may only download files of systems in
    that scope. Before, /reports/ checked only that the path stayed inside
    reports/, so a single-customer user could fetch another customer's
    sheets and screenshots by name. Unattributable files: refused to scoped users.
    """
    from core import auth
    user = auth.current_user.get()
    if user is None or (user.get("customers") is None and user.get("systems") is None):
        return                                   # admin / unscoped / local admin
    from core.config_loader import get_all_systems
    owner = _report_owner(rel_path, get_all_systems())
    allowed = {s.get("name") for s in get_systems()}
    if owner is None or owner not in allowed:
        raise HTTPException(status_code=404, detail="Not found")

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


_rca_hold_logged: dict = {}


def _rca_auto_block(system_name: str) -> str | None:
    """
    Why an AUTOMATIC RCA must not drive SAP GUI on this system now, or None.
    The Performance monitoring button is not held by any of these.
    """
    # A manual run's hold (manual_pause) leaves `enabled` as it was, so
    # `enabled` alone says whether automatic monitoring is paused.
    if not _scheduler_state.get("enabled"):
        return "automatic monitoring is paused"
    snooze = _scheduler_state.get("snooze_until")
    if snooze and datetime.now() < snooze:
        return f"automatic monitoring is snoozed until {snooze:%H:%M}"
    hold = _stop_hold_reason(system_name)
    if hold:
        return hold
    # parked (e.g. the logon was refused: another logon could lock the SAP
    # user), in maintenance, on standby, or its VPN switched off on purpose
    return _sweep_block_reason(system_name)


def _rca_observe(system_name: str, payload: dict) -> None:
    if _rca_trigger is None:
        _rca_load()
    d = _rca_trigger.observe(system_name, payload)
    if d.fire:
        block = _rca_auto_block(system_name)
        if block:
            _rca_trigger.cancel_fired(system_name)
            last = _rca_hold_logged.get(system_name)
            if not last or (datetime.now() - last) > timedelta(hours=1):
                _rca_hold_logged[system_name] = datetime.now()
                log.info(f"{system_name}: automatic RCA not started -- {block} ({d.reason})")
                try:
                    from core import rca_log
                    rca_log.not_started(system_name, d.reason, block)
                except Exception:  # noqa: BLE001
                    pass
            return
        log.warning(f"{system_name}: automatic RCA -- {d.reason}")
        started = _rca_start(system_name, d.reason, manual=False)
        if not started.get("started"):
            # Not started (another RCA is running): do not let this count as
            # the system's RCA for the hour -- the next reading may fire.
            _rca_trigger.cancel_fired(system_name)
            log.warning(f"{system_name}: automatic RCA not started -- {started.get('reason')}")
            return
        try:
            # Shared cooldown: the sweep path (main.py) must not start a
            # second RCA for the same spike within the hour.
            from core.rca_trigger import mark_fired_on_disk
            mark_fired_on_disk(system_name, d.reason)
        except Exception as exc:   # noqa: BLE001
            log.debug(f"RCA state not shared: {exc}")


def _rca_start(system_name: str, reason: str, manual: bool) -> dict:
    """Launch the RCA in a thread. Returns immediately with what happened."""
    if _rca_trigger is None:
        _rca_load()
    with _rca_lock:
        if _rca_state["running"]:
            return {"started": False, "reason": f"RCA already running on {_rca_state['system']}"}
        _rca_state.update({"running": True, "system": system_name,
                           "started_at": datetime.now().isoformat(timespec="seconds")})
        _rca_cancel["on"] = False              # a Stop from now on cancels this RCA
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
    _rca_started = datetime.now()
    try:
        cfg = next((c for c in get_systems() if c.get("name") == system_name), None)
        if cfg is None:
            outcome["error"] = f"Unknown system: {system_name}"
            return

        # ---- whatever is holding SAP GUI ----------------------------------
        # An AUTOMATIC RCA never stops monitoring: a scheduled or manual run is
        # what someone asked for (a 09:00 daily run was cancelled by an RCA
        # and the day's run was lost). It waits for the run to finish
        # (IBO_RCA_WAIT_FOR_SWEEP_MIN, default 20) and otherwise gives up.
        # Only the Performance monitoring button (manual) may pre-empt.
        manual_rca = str(reason).lower().startswith("manual")
        if not manual_rca and _run_state["running"]:
            try:
                wait_min = float(os.getenv("IBO_RCA_WAIT_FOR_SWEEP_MIN", "20") or 20)
            except ValueError:
                wait_min = 20.0
            busy_with = _run_state.get("current_system")
            outcome["waited_for"] = busy_with
            deadline = time_module.monotonic() + wait_min * 60
            while _run_state["running"] and time_module.monotonic() < deadline and not _rca_cancel["on"]:
                time_module.sleep(2)
            if _run_state["running"]:
                outcome["error"] = (f"monitoring of {busy_with} was running for more than {wait_min:.0f} min; "
                                    f"automatic RCA not started (monitoring has priority)")
                log.warning(f"RCA on {system_name} not started: {outcome['error']}")
                return
        if manual_rca and _rca_trigger.cfg.preempt_running_sweep:
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
            if _rca_cancel["on"]:
                outcome["error"] = "stopped from the dashboard before SAP GUI was opened"
                log.info(f"RCA on {system_name} not run: stopped from the dashboard while it waited")
                return
            if _run_state["running"]:
                outcome["error"] = "sweep restarted before RCA could take the session"
                return
            _run_state.update({"running": True, "error": None, "cancel_requested": False,
                               "stop_requested": False,
                               "current_system": system_name, "profile_id": "rca"})
        try:
            # The same two SAP GUI guards a sweep uses (main.py): an abandoned
            # sweep thread may still be inside SAP GUI, and another process
            # (the collector) may hold the machine-wide sweep lock. The
            # simulation showed an RCA opening SAP GUI next to an abandoned
            # sweep thread -- two drivers on one desktop.
            try:
                import main as _main
                zombie = _main.abandoned_gui_alive()
            except Exception:  # noqa: BLE001
                zombie = None
            if zombie:
                outcome["error"] = (f"SAP GUI is still held by an abandoned {zombie.get('system')} run; "
                                    "RCA not started (restart the dashboard if this persists)")
                log.error(f"RCA on {system_name} not started: {outcome['error']}")
                return
            from core.run_lock import sweep_lock
            with sweep_lock(f"rca:{system_name}") as acquired:
                if not acquired:
                    outcome["error"] = "another process is running a sweep (machine-wide SAP GUI lock); RCA not started"
                    log.warning(f"RCA on {system_name} not started: {outcome['error']}")
                    return
                outcome.update(run_rca(cfg, reason, cfg=_rca_cfg,
                                       should_stop=lambda: bool(_rca_cancel["on"])))
            outcome["ok"] = True
        finally:
            with _run_lock:
                _run_state.update({"running": False, "current_system": None, "profile_id": None})
    except Exception as exc:   # noqa: BLE001
        # SAP GUI not available (scripting off, Logon Pad not in front, the
        # desktop in use) is an expected condition, recorded in the RCA log
        # with its reason; anything else is a fault for the Errors page.
        if (type(exc).__module__ or "").startswith("sap_gui") or isinstance(exc, TimeoutError) or \
                "SAP GUI" in str(exc) or "Logon Pad" in str(exc):
            log.warning(f"RCA on {system_name} could not use SAP GUI: {type(exc).__name__}: {exc}")
        else:
            log.error(f"RCA run failed for {system_name}: {type(exc).__name__}: {exc}")
        outcome["error"] = f"RCA failed ({_error_ref(exc, 'RCA run ' + system_name)})"
    finally:
        outcome["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if not outcome.get("rca_log_id") and outcome.get("error"):
            try:                      # asked for but never reached the pipeline: say why
                from core import rca_log
                rca_log.not_started(system_name, reason, outcome["error"])
            except Exception:  # noqa: BLE001
                pass
        with _rca_lock:
            _rca_state.update({"running": False, "system": None, "last": outcome})
        try:
            from core import run_history
            run_history.record(system=system_name, trigger="rca", started_at=_rca_started,
                               ended_at=datetime.now(),
                               result="success" if outcome.get("ok") else
                               "stopped-by-user" if _rca_cancel.get("on") else "failed",
                               failure_reason=None if outcome.get("ok") else outcome.get("error"),
                               note=f"performance RCA: {reason}"[:300])
        except Exception as exc:  # noqa: BLE001
            log.debug(f"run history (RCA) not written: {exc}")


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

"""
RFC collector — SAP telemetry over pyrfc.

WHY THIS EXISTS
---------------
This codebase monitored SAP through GUI scripting + OCR and through SSH.
Both work, but both have limits: GUI scripting needs a Windows host with SAP
GUI installed and a foreground session, and SSH needs OS-level access, which
RISE and hosted systems do not grant.

RFC needs neither. It reads the same T-code data directly from the
application server over the network, so it works headless, on any OS, and on
systems where only a service account exists.

The collector prefers the custom function module Z_GET_OBSERVABILITY_DATA
(see abap/) which returns ~20 counters in a single round trip. Where that is
not installed it falls back to RFC_READ_TABLE and standard function modules,
so a system with no Z-transport still reports.

DESIGN RULES CARRIED OVER FROM THE RFC BRANCH
---------------------------------------------
1. "Could not read" is never 0. A metric that could not be collected gets
   Status.UNKNOWN and value None. Conflating a failed read with a healthy
   zero is what made an unreachable production system display a perfect
   score in the previous codebase.
2. One connection per cycle. The previous version opened a fresh RFC logon
   per dashboard card — roughly 25 logons per system per minute, which fills
   the security audit log and risks locking the monitoring account.
3. Failure cooldown. A system that just failed is parked, so a dead host does
   not block every request for the full TCP timeout (~21s on Windows).
4. Credentials come only from configuration. No inline hosts or passwords.
5. Route strings are passed through untouched. Reformatting a SAProuter route
   breaks it — a complete route ends in '/H/' so the library can append the
   target host, and it may carry a '/W/<secret>/' router password.
"""

from __future__ import annotations

import json as _json
import os as _os
import re as _re
import threading
import time
from datetime import datetime, timedelta

from core.models import MetricResult, Status
from utils.logger import get_logger, LOG_DIR as _LOG_DIR
from collectors import rfc_logon_guard as _guard
from collectors import rfc_table as _rfc_table
from collectors import sap_clock as _sap_clock
from collectors.rfc_logon_guard import clear_logon_block, logon_block_status  # noqa: F401 (re-export)

# Round 32: this module called log.warning() (WHERE-clause splitter) without
# ever defining `log` -- that path would have raised NameError, not warned.
log = get_logger(__name__)

try:
    from pyrfc import Connection
    PYRFC_AVAILABLE = True
except ImportError:  # pyrfc needs the NetWeaver RFC SDK; degrade quietly.
    Connection = None
    PYRFC_AVAILABLE = False


# --------------------------------------------------------------------------
# Failure cooldown
# --------------------------------------------------------------------------

_COOLDOWN_TIMEOUT = 300   # unreachable host / TCP timeout
_COOLDOWN_ROUTE = 300     # SAProuter refused the route
_COOLDOWN_AUTH = 900      # bad credentials — retrying burns lockout attempts
_COOLDOWN_DEFAULT = 120

_cooldowns: dict[str, tuple] = {}          # system -> (until monotonic, error, parked at wall time)
_lock = threading.Lock()

# Hard stop after repeated logon failures. Each rejected logon moves the RFC
# user closer to login/fails_to_user_lock on the CLIENT system (default 5);
# a 15-minute cooldown only spreads the attempts out. After this many in a
# row the system is frozen until its password changes or clear_cooldown()
# is called (the Systems page does that when credentials are edited).
_AUTH_MAX_FAILURES = int(_os.getenv("IBO_RFC_AUTH_MAX_FAILURES", "3") or 3)
_auth_failures: dict[str, int] = {}
_auth_frozen: dict[str, str] = {}      # system -> error text
_frozen_at: dict[str, float] = {}      # system -> wall time it was frozen


def _is_auth_error(error_text: str) -> bool:
    low = (error_text or "").lower()
    return any(t in low for t in ("password is incorrect", "logon", "locked",
                                  "rfc_logon_failure", "not authorized"))


def _log_frozen(text: str) -> None:
    try:
        from utils.logger import get_logger
        get_logger(__name__, "monitoring").error(text)
    except Exception:
        pass


def auth_frozen(system: str) -> str | None:
    """Error text if `system` is frozen for repeated logon failures, else None."""
    with _lock:
        return _auth_frozen.get(system)


def is_credential_error(error_text: str | None) -> bool:
    """
    True when the RFC logon was refused because of the credentials, not the
    network. Repeating such a logon burns the user's failed-attempt budget
    (login/fails_to_user_lock) and locks the monitoring user on the
    customer's system -- the one outcome worse than not monitoring.
    """
    low = (error_text or "").lower()
    return any(t in low for t in ("name or password", "password", "logon",
                                  "locked", "not authorized", "user is locked",
                                  "password logon no longer possible"))


def _connect_error_text(system: str, cfg: dict, raw: str) -> str:
    """Tunnelled systems: say whether the VPN or the system is down (core/reachability)."""
    try:
        from core.reachability import diagnose_connect_error
        return diagnose_connect_error(system, cfg, raw) or raw
    except Exception:
        return raw


def _credentials_text(system: str, raw: str) -> str:
    """'[CREDENTIALS] PS4 is UP, but ...' -- keeps the SAP text, so the
    auth-freeze matching on 'password'/'logon' still works."""
    try:
        from core.reachability import credentials_message
        return credentials_message(system, raw)
    except Exception:
        return raw


def _reachable_again(system: str, cfg: dict) -> None:
    try:
        from core.reachability import report_ok, auto_unpark_after_logon
        report_ok(system, cfg)
        auto_unpark_after_logon(system)       # the logon worked: a refused-logon park is over
    except Exception:
        pass


def _connection_lost(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {getattr(exc, 'key', '')} {exc}".upper()
    return any(t in text for t in ("COMMUNICATION_FAILURE", "CONNECTION CLOSED", "CONNECTION_BROKEN",
                                   "RFC_CLOSED", "RFC_INVALID_HANDLE", "CPIC", "NETWORK"))


def _cooldown_for(error_text: str) -> int:
    low = (error_text or "").lower()
    if "no rfc configuration" in low:
        return _COOLDOWN_DEFAULT      # nothing was dialled: no lockout risk, just a missing setting
    if any(t in low for t in ("timed out", "wsaetimedout", "not reached", "refused")):
        return _COOLDOWN_TIMEOUT
    if "route permission denied" in low or "saprouter" in low:
        return _COOLDOWN_ROUTE
    if any(t in low for t in ("logon", "password", "locked", "not authorized")):
        return _COOLDOWN_AUTH
    return _COOLDOWN_DEFAULT


def _secrets_changed_since(wall: float) -> bool:
    try:
        from core.config_loader import secrets_changed_at
        return secrets_changed_at() > wall
    except Exception:  # noqa: BLE001
        return False


def cooldown_remaining(system: str) -> tuple[int, str | None]:
    with _lock:
        entry = _cooldowns.get(system)
        parked_at = entry[2] if entry and len(entry) > 2 else None
        frozen_at = _frozen_at.get(system)
    # A password was changed on the Settings page after this system was
    # parked: try again now instead of waiting out the old failure.
    if (parked_at is not None and _secrets_changed_since(parked_at)) or \
            (frozen_at is not None and _secrets_changed_since(frozen_at)):
        clear_cooldown(system)
    with _lock:
        frozen = _auth_frozen.get(system)
        entry = _cooldowns.get(system)
    if frozen:
        # Effectively infinite: nothing will dial this system until the
        # credentials are changed.
        return 10 ** 9, frozen
    if not entry:
        return 0, None
    until, error = entry[0], entry[1]
    left = until - time.monotonic()
    if left <= 0:
        with _lock:
            _cooldowns.pop(system, None)
        return 0, None
    return int(left), error


def _park(system: str, error_text: str) -> None:
    # Alert (VPN / DOWN / credentials) before the cooldown bookkeeping; the
    # alert ledger throttles repeats. Never raises.
    try:
        from core.reachability import report
        report(system, error_text)
    except Exception:
        pass
    seconds = _cooldown_for(error_text)
    with _lock:
        _cooldowns[system] = (time.monotonic() + seconds, error_text, time.time())
        if _is_auth_error(error_text):
            n = _auth_failures.get(system, 0) + 1
            _auth_failures[system] = n
            if n >= _AUTH_MAX_FAILURES and system not in _auth_frozen:
                _auth_frozen[system] = (
                    f"Frozen after {n} logon failures to protect the RFC user "
                    f"from lockout. Fix the credentials for {system} in .env "
                    f"(or edit the system) to resume. Last error: {error_text}")
                _frozen_at[system] = time.time()
                _log_frozen(_auth_frozen[system])
        else:
            _auth_failures.pop(system, None)


def note_success(system: str) -> None:
    """A successful logon resets the failure counter."""
    with _lock:
        _auth_failures.pop(system, None)
        _auth_frozen.pop(system, None)
        _frozen_at.pop(system, None)


def clear_cooldown(system: str | None = None) -> None:
    """Retry immediately — call after credentials change."""
    with _lock:
        if system is None:
            _cooldowns.clear(); _auth_failures.clear(); _auth_frozen.clear(); _frozen_at.clear()
        else:
            _cooldowns.pop(system, None)
            _auth_failures.pop(system, None)
            _auth_frozen.pop(system, None)
            _frozen_at.pop(system, None)


def park(system: str, error_text: str) -> None:
    """
    Public entry point to the failure cooldown.

    _park() used to be reachable only from collect_rfc_metrics(), i.e. from
    the scheduled sweep. The live refresher called cooldown_remaining() but
    nothing on that path ever WROTE a cooldown, so an unreachable host was
    re-dialled on every single refresh pass. On Windows a TCP connect to a
    host that does not answer sits for roughly 21 seconds before failing,
    and the refresher waits for every system before sleeping -- so two dead
    systems stretched an 8-second cycle to well over 20 and made every
    healthy system's reading that stale.
    """
    _park(system, error_text)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------

def _format_saprouter(raw) -> str:
    """
    Returns the route string as stored, adding only a leading '/'.

    Do NOT normalise further. An earlier version stripped the trailing slash
    and appended '/S/3299/H', which corrupted valid routes and produced
    'NiPGetHostByName: H/<target> not found'.
    """
    if not raw:
        return ""
    route = str(raw).strip()
    if not route:
        return ""
    return route if route.startswith("/") else f"/{route}"


def build_rfc_params(cfg: dict) -> dict | None:
    """Builds pyrfc connection kwargs from a systems.yaml entry."""
    rfc = cfg.get("rfc") or {}
    user = (rfc.get("username") or "").strip()
    passwd = (rfc.get("password") or "").strip()
    host = (rfc.get("ashost") or "").strip()
    if not (user and passwd and host):
        return None

    params = {
        "ashost": host,
        "sysnr": str(rfc.get("sysnr", "00")).zfill(2),
        "client": str(rfc.get("client") or cfg.get("client") or "000"),
        "user": user,
        "passwd": passwd,
        "lang": rfc.get("language") or cfg.get("language") or "EN",
    }
    route = _format_saprouter(rfc.get("saprouter"))
    if route:
        params["saprouter"] = route

    # Optional separate gateway (for example the local end of an SSH port
    # forward). ashost stays the real application server; only the gateway
    # the client talks to changes.
    for key in ("gwhost", "gwserv"):
        value = str(rfc.get(key) or "").strip()
        if value:
            params[key] = value

    # NOTE: NWRFC_CONNECT_TIMEOUT / NWRFC_COMM_TIMEOUT used to be added here.
    # They are not SAP NW RFC SDK connection parameters -- the SDK ignored
    # them, so no call had a timeout at all. The call timeout is now pyrfc's
    # own client-side timeout (Connection(config={"timeout": N}), see
    # rfc_call_timeout()); the connect attempt is bounded by the TCP probe in
    # SapSession.__enter__. The SDK has no logon-timeout connection parameter.
    params["CPIC_MAX_CONV"] = "50"
    return params


class LogonBlocked(RuntimeError):
    """Raised by guarded_connect() when the logon guard refuses to log on."""


def guarded_connect(system: str, params: dict, cfg: dict | None = None):
    """
    A raw pyrfc Connection for callers that do not use SapSession (the
    synthetic logon probe in core/synthetic.py). Goes through the same
    cross-process logon guard and call timeout. Raises LogonBlocked (whose
    text names the fix) or the original connect error.
    """
    if Connection is None:
        raise RuntimeError("pyrfc not installed")
    timeout = rfc_call_timeout(cfg)
    target = _guard.LogonTarget.from_rfc_params(system, params)

    def _open():
        return Connection(config={"timeout": timeout}, **params) if timeout else Connection(**params)

    conn, error, blocked = _guard.guarded_logon(target, _open)
    if conn is None:
        raise (LogonBlocked(error) if blocked else RuntimeError(error))
    return conn


_comm_timeout_warned = False


def rfc_call_timeout(cfg: dict | None = None) -> int | None:
    """
    Per-call timeout in seconds for pyrfc (config={"timeout": N}). When it
    expires pyrfc cancels the call (RfcCancel) and raises; SapSession then
    marks the connection dead and stops calling it.

    systems.yaml rfc.call_timeout, else IBO_RFC_CALL_TIMEOUT, else 120 s (a
    STAT frame read can take ~40 s). 0 disables it. The old
    IBO_RFC_COMM_TIMEOUT is NOT used for this: old .env files set it to 30,
    which cancelled ~40 s STAT reads (logged once when it is set).
    """
    global _comm_timeout_warned
    if not _comm_timeout_warned and (_os.environ.get("IBO_RFC_COMM_TIMEOUT") or "").strip():
        _comm_timeout_warned = True
        log.info("IBO_RFC_COMM_TIMEOUT is set but no longer used for the RFC call timeout; "
                 "set IBO_RFC_CALL_TIMEOUT (default 120 s) or rfc.call_timeout instead.")
    rfc = (cfg or {}).get("rfc") or {}
    raw = rfc.get("call_timeout")
    if raw is None or str(raw).strip() == "":
        raw = _os.environ.get("IBO_RFC_CALL_TIMEOUT") or "120"
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        value = 120
    return value if value > 0 else None


def _tcp_reachable(host: str, params: dict, cfg: dict) -> tuple[bool, str]:
    """
    Can we open a TCP socket to the app server's dispatcher/gateway port?

    A cheap gate in front of the RFC logon so an unreachable host fails in
    under a second instead of on the OS TCP timeout. The port is the SAP
    dispatcher port for the instance: 33NN where NN is the system number.
    Derived from sysnr when present, else from an explicit rfc.port, else the
    common 3300.
    """
    import socket

    rfc = cfg.get("rfc") or {}
    port = None
    sysnr = str(params.get("sysnr") or rfc.get("sysnr") or "").strip()
    if sysnr.isdigit():
        port = 3300 + int(sysnr)
    if port is None:
        try:
            port = int(str(rfc.get("port") or "3300").strip())
        except ValueError:
            port = 3300

    # Through a SAProuter the first hop is the router; test that.
    try:
        from core.reachability import router_first_hop
        _hop = router_first_hop(params.get("saprouter"))
    except Exception:  # noqa: BLE001
        _hop = None
    if _hop:
        host, port = _hop
    # When a separate gateway is configured the connection goes there, so
    # that is the address the reachability check must test.
    gw_host = "" if _hop else str(params.get("gwhost") or "").strip()
    if gw_host:
        host = gw_host
    gw_serv = "" if _hop else str(params.get("gwserv") or "").strip()
    if gw_serv.isdigit():
        port = int(gw_serv)

    # A system reached through a tunnel whose far-end VPN is known to be down:
    # say so now. The local tunnel port would answer anyway.
    try:
        from core.reachability import precheck
        _pre = precheck(str(cfg.get("name") or host), cfg)
        if _pre:
            return False, _pre
    except Exception:
        pass

    timeout = float(_os.environ.get("IBO_RFC_TCP_PROBE_TIMEOUT", "2") or 2)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        # Say WHY in operator terms: VPN not connected vs system down vs not
        # enough evidence to tell (core/reachability.py). The old generic text
        # stays as the fallback if the diagnosis itself fails.
        try:
            from core.reachability import diagnose_unreachable
            return False, diagnose_unreachable(str(cfg.get("name") or host), cfg, host, port,
                                               exc.__class__.__name__)
        except Exception:
            return False, (f"host {host}:{port} not reachable "
                           f"({exc.__class__.__name__}) -- network/Basis check, "
                           f"not a code issue")


# ---------------------------------------------------------------------------
# Connection pool
#
# Each live poll used to LOG ON and LOG OFF: one RFC logon every few seconds
# per system, all day. On a customer system with the security audit log on,
# that is thousands of audit entries a day attributed to our user, and a
# logon is also the slowest part of a read. The pool keeps one open
# connection per system and hands it to whichever thread asks, one at a
# time. Idle connections are closed after IBO_RFC_IDLE_SECONDS (default
# 900) so a sweep-only process does not hold a session open for hours.
# A connection that died (network, system restart) is detected with
# alive() and replaced. IBO_RFC_POOL=0 restores logon-per-read.
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: dict[str, dict] = {}          # system -> {"conn", "lock", "last_used", "sig"}


def _pool_enabled() -> bool:
    return (_os.getenv("IBO_RFC_POOL", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def _idle_seconds() -> float:
    try:
        return float(_os.getenv("IBO_RFC_IDLE_SECONDS", "900") or 900)
    except ValueError:
        return 900.0


def _params_sig(params: dict) -> str:
    return "|".join(str(params.get(k, "")) for k in ("ashost", "sysnr", "client", "user", "saprouter"))


def _close_conn(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def close_idle_connections(max_idle: float | None = None) -> int:
    """Close pooled connections unused for longer than max_idle. Returns count."""
    max_idle = _idle_seconds() if max_idle is None else max_idle
    closed = 0
    now = time.monotonic()
    with _pool_lock:
        for system, entry in list(_pool.items()):
            if entry.get("conn") is None or now - entry.get("last_used", now) < max_idle:
                continue
            if not entry["lock"].acquire(blocking=False):
                continue          # in use right now
            try:
                _close_conn(entry["conn"])
                entry["conn"] = None
                closed += 1
                log.info(f"{system}: idle RFC connection closed")
            finally:
                entry["lock"].release()
    return closed


def close_all_connections() -> None:
    with _pool_lock:
        for entry in _pool.values():
            if entry.get("conn") is not None:
                _close_conn(entry["conn"])
                entry["conn"] = None


class SapSession:
    """
    One RFC session for one read. `ok` is False when the system is unreachable.

    With the pool on (default) the underlying connection is reused across
    reads; `with SapSession(...)` still brackets one read, and the session
    holds the system's pool lock for that long so two threads never share
    one RFC handle.
    """

    def __init__(self, system: str, cfg: dict):
        self.system = system
        self.cfg = cfg or {}
        self.conn = None
        self.ok = False
        self.error: str | None = None
        # Round 32: the reason the most recent call() failed, "" when it did
        # not. call() still never raises; this is how a caller finds out WHY.
        self.last_error: str = ""
        # True when the logon itself was refused for a credential reason.
        self.auth_failed: bool = False
        self._pooled: dict | None = None
        # Set when a call failed in a way that means the connection is gone.
        self._dead: bool = False
        # True when the logon guard refused to log on (a previous logon with
        # this password failed). The operator must fix .env and clear it.
        self.logon_blocked = False
        # True when a call hit a communication failure / timeout and the
        # connection was closed. Every later call() returns None at once.
        self.connection_lost = False
        # Metadata of the most recent read_table(): partial, row_cap, ...
        self.last_read: dict = {}

    def __enter__(self):
        if not PYRFC_AVAILABLE:
            self.error = "pyrfc not installed (NetWeaver RFC SDK required)"
            return self
        params = build_rfc_params(self.cfg)
        if not params:
            self.error = "no RFC configuration (ashost, username or password missing)"
            return self

        # LOGON GUARD FIRST. A key (host, sysnr, client, user, password
        # fingerprint) whose logon was refused is blocked in every process
        # until the password changes or an operator clears it. Checked before
        # anything touches the network.
        target = _guard.LogonTarget.from_rfc_params(self.system, params)
        block = _guard.is_blocked_now(target)
        if block:
            self.error = _guard.blocked_message(self.system, block)
            self.logon_blocked = True
            return self

        # FAST REACHABILITY PRE-CHECK.
        #
        # NWRFC_CONNECT_TIMEOUT is honoured by the RFC library only once it
        # has a socket; it does NOT bound the initial TCP connect. So a host
        # that is powered off or firewalled (SARLOHA PRD, CEQ) still made
        # Connection() sit on the OS TCP timeout -- ~21s on Windows -- and
        # since the refresher waits for every system, two dead hosts set the
        # pace for all of them. A plain socket connect with a short timeout
        # turns that 21s hang into a sub-second failure, and only then do we
        # reach for the (much heavier) RFC logon.
        #
        # Skipped when a SAProuter is in play: the route target is not the
        # ashost, so a direct probe would test the wrong endpoint.
        host = params.get("ashost")
        if host and not params.get("saprouter"):
            reachable, why = _tcp_reachable(host, params, self.cfg)
            if not reachable:
                self.error = why
                return self

        self._target = target
        if _pool_enabled():
            return self._enter_pooled(params)

        conn = self._open_connection(params)
        if conn is not None:
            self.conn = conn
            self.ok = True
            _reachable_again(self.system, self.cfg)
        return self

    def _open_connection(self, params: dict):
        """
        The ONE place an RFC logon happens (pooled or not): through the
        cross-process logon guard, with pyrfc's call timeout. Returns the
        connection, or None with self.error / auth_failed / logon_blocked set.
        """
        timeout = rfc_call_timeout(self.cfg)

        def _open():
            if timeout:
                # pyrfc >= 2.5 (requirements pin 3.3): client-side call
                # timeout; on expiry the call is cancelled and raises.
                return Connection(config={"timeout": timeout}, **params)
            return Connection(**params)

        target = getattr(self, "_target", None) or _guard.LogonTarget.from_rfc_params(self.system, params)
        conn, error, blocked = _guard.guarded_logon(target, _open)
        if conn is not None:
            return conn
        self.logon_blocked = bool(blocked)
        self.error = error
        self.auth_failed = bool(blocked) or is_credential_error(error)
        if blocked:
            pass                      # the guard's message says what to do
        elif self.auth_failed:
            self.error = _credentials_text(self.system, error)
        else:
            self.error = _connect_error_text(self.system, self.cfg, error)
        return None

    def _enter_pooled(self, params: dict):
        sig = _params_sig(params)
        with _pool_lock:
            entry = _pool.setdefault(self.system, {"conn": None, "lock": threading.Lock(),
                                                   "last_used": time.monotonic(), "sig": sig})
        # Serialise reads of one system. 120s covers a slow deep read; a
        # caller that cannot get the lock reports "busy" instead of piling up.
        if not entry["lock"].acquire(timeout=120):
            self.error = "RFC connection busy (another read of this system has not returned)"
            return self
        self._pooled = entry
        conn = entry.get("conn")
        if conn is not None and entry.get("sig") != sig:
            _close_conn(conn)           # credentials or host changed in config
            conn = None
        if conn is not None:
            try:
                if not conn.alive():
                    _close_conn(conn)
                    conn = None
            except Exception:
                _close_conn(conn)
                conn = None
        if conn is None:
            conn = self._open_connection(params)
            if conn is None:
                entry["conn"] = None
                entry["lock"].release()
                self._pooled = None
                return self
            entry["conn"] = conn
            entry["sig"] = sig
            log.info(f"{self.system}: RFC connection opened (pooled)")
            _reachable_again(self.system, self.cfg)
        self.conn = conn
        self.ok = True
        return self

    def __exit__(self, *exc):
        self._close()
        return False

    def _close(self) -> None:
        entry = getattr(self, "_pooled", None)
        if entry is not None:
            if self._dead:
                _close_conn(entry.get("conn"))
                entry["conn"] = None
            entry["last_used"] = time.monotonic()
            self.conn = None
            self._pooled = None
            entry["lock"].release()
            # Piggy-back the idle reaper on every release: no extra thread,
            # and a system nobody reads for 15 minutes gets its session closed.
            try:
                close_idle_connections()
            except Exception:
                pass
            return
        if self.conn is not None:
            _close_conn(self.conn)
            self.conn = None

    # Errors after which the connection handle must not be used again.
    _DEAD_CLASSES = frozenset({"CommunicationError", "LogonError"})
    _DEAD_KEYS = frozenset({"RFC_COMMUNICATION_FAILURE", "RFC_CANCELED", "RFC_TIMEOUT",
                            "RFC_INVALID_HANDLE", "RFC_CLOSED", "RFC_LOGON_FAILURE"})
    _DEAD_TEXT = ("connection was canceled", "connection was cancelled", "rfc_canceled",
                  "rfc_communication_failure", "rfc_invalid_handle", "timeout",
                  "timed out", "connection closed", "connection reset")

    @classmethod
    def _is_dead_connection_error(cls, exc: Exception) -> bool:
        if type(exc).__name__ in cls._DEAD_CLASSES:
            return True
        key = cls._error_key(exc)
        if key in cls._DEAD_KEYS:
            return True
        low = str(exc).lower()
        # An ABAP TIME_OUT dump is the server's own limit on one call; the
        # handle survives that, so do not read "TIME_OUT" as a dead line.
        if "time_out" in low and type(exc).__name__.startswith("ABAP"):
            return False
        return any(t in low for t in cls._DEAD_TEXT)

    def _mark_dead(self, reason: str, exc: Exception | None = None) -> None:
        """Stop using this connection for the rest of the collection."""
        self.ok = False
        self._dead = True               # a pooled handle is closed, not reused
        self.connection_lost = True
        self.error = f"connection lost: {reason}"[:400]
        if exc is not None and _guard.is_auth_failure(exc) and PYRFC_AVAILABLE:
            params = build_rfc_params(self.cfg)
            if params:
                block = _guard.record_failure(
                    _guard.LogonTarget.from_rfc_params(self.system, params), reason)
                self.logon_blocked = True
                self.error = _guard.blocked_message(self.system, block)
        self._close()
        log.warning(f"[{self.system}] RFC connection closed after failure; no further "
                    f"calls this cycle -- {reason[:200]}")

    # Distinct failures already written to the log, per process. The live
    # wall polls every minute; the same FU_NOT_FOUND on every poll would
    # bury everything else in application.log.
    _failures_logged: set = set()
    _failures_lock = threading.Lock()

    # Round 34: function modules a system does not have. SAP answers
    # FU_NOT_FOUND, and that answer does not change until someone transports
    # the FM in. Asking again on every poll cost 4-5 wasted round trips per
    # system per minute (Z_GET_OBSERVABILITY_DATA, Z_GET_LOGON_LOAD,
    # RZL_INTG_READALL_C, TH_(GET_)LOAD_DISTRIBUTION: ~92,000 errors in
    # dev_rfc.log since 01.09). Remembered per (system, FM) for
    # MISSING_FM_RECHECK_SECONDS, so an FM installed later is picked up
    # within hours without a restart.
    MISSING_FM_RECHECK_SECONDS = 6 * 3600
    _missing_fms: dict = {}          # (system, FM) -> epoch seconds noted

    # Round 35: shared between processes. The live wall reads systems in a
    # pool of worker processes and the sweep runs in its own; each learned
    # the same FU_NOT_FOUND separately. One small JSON file in logs/ lets
    # them share it, and it survives a dashboard restart.
    MISSING_FM_FILE = _os.path.join(_LOG_DIR, "rfc_missing_functions.json")
    _missing_mtime: float | None = None

    # Answers that mean "nothing to report", not "something is broken".
    # Logged at DEBUG only: /SDF/GET_DUMP_LOG says NO_DATA_FOUND on every
    # dump-free poll, TH_GET_VIRT_SERVER says NOT_FOUND on single-host setups.
    QUIET_KEYS = frozenset({"NO_DATA_FOUND", "NOT_FOUND", "TABLE_WITHOUT_DATA"})

    @classmethod
    def _missing_load(cls) -> None:
        """Merge the shared file into memory when it has changed. Caller holds the lock."""
        try:
            mtime = _os.path.getmtime(cls.MISSING_FM_FILE)
        except OSError:
            return
        if mtime == cls._missing_mtime:
            return
        try:
            with open(cls.MISSING_FM_FILE, encoding="utf-8") as fh:
                data = _json.load(fh)
            for k, v in (data or {}).items():
                system, _, fm = str(k).partition("|")
                if system and fm:
                    cls._missing_fms[(system, fm)] = max(float(v), cls._missing_fms.get((system, fm), 0.0))
            cls._missing_mtime = mtime
        except Exception:
            pass

    @classmethod
    def _missing_save(cls) -> None:
        """Write the still-valid entries back. Caller holds the lock. Never raises."""
        now = time.time()
        data = {f"{s}|{fm}": ts for (s, fm), ts in cls._missing_fms.items()
                if now - ts < cls.MISSING_FM_RECHECK_SECONDS}
        tmp = f"{cls.MISSING_FM_FILE}.{_os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(data, fh, indent=1, sort_keys=True)
            _os.replace(tmp, cls.MISSING_FM_FILE)
            cls._missing_mtime = _os.path.getmtime(cls.MISSING_FM_FILE)
        except Exception:
            try:
                _os.remove(tmp)
            except OSError:
                pass

    @classmethod
    def forget_missing_functions(cls, system: str | None = None) -> None:
        """Ask SAP again: for one system, or for all of them."""
        with cls._failures_lock:
            cls._missing_load()
            for key in [k for k in cls._missing_fms if system is None or k[0] == system]:
                cls._missing_fms.pop(key, None)
            cls._missing_save()

    @staticmethod
    def _error_key(exc: Exception) -> str:
        """SAP's exception key (FU_NOT_FOUND, NO_DATA_FOUND, ...), or ""."""
        key = str(getattr(exc, "key", "") or "")
        if key:
            return key
        m = _re.search(r"(?i)key[=:]\s*([A-Z0-9_]+)", str(exc))
        return m.group(1).upper() if m else ""

    @classmethod
    def _is_fu_not_found(cls, exc: Exception) -> bool:
        # A function that exists but is not remote-enabled will not become
        # callable either until someone changes it in SE37 -- same handling.
        return cls._error_key(exc) in ("FU_NOT_FOUND", "CALL_FUNCTION_NOT_REMOTE") or \
            "FU_NOT_FOUND" in str(exc) or "CALL_FUNCTION_NOT_REMOTE" in str(exc)

    def call(self, name: str, **kwargs):
        """
        Returns the RFC result, or None on failure. Never raises.

        The reason is kept in self.last_error (round 32). Round 35 logging:
          * FU_NOT_FOUND -- INFO, once, when first learned (any process);
          * NO_DATA_FOUND / NOT_FOUND -- DEBUG: an empty answer, not a fault;
          * anything else -- WARNING, once per process per
            (system, function, table, SAP key).
        """
        self.last_error = ""
        if not self.ok:
            self.last_error = f"no RFC session ({self.error or 'not connected'})"
            return None
        key = (self.system, name)
        now = time.time()
        with SapSession._failures_lock:
            SapSession._missing_load()
            noted = SapSession._missing_fms.get(key)
            if noted is not None and now - noted >= self.MISSING_FM_RECHECK_SECONDS:
                SapSession._missing_fms.pop(key, None)
                noted = None
        if noted is not None:
            # Same wording callers already look for ("FU_NOT_FOUND").
            self.last_error = f"{name}: FU_NOT_FOUND (not installed on {self.system}; not asked again)"
            return None
        try:
            return self.conn.call(name, **kwargs)
        except Exception as exc:
            err_key = self._error_key(exc)
            what = name
            if kwargs.get("QUERY_TABLE"):
                what = f"{name}({kwargs['QUERY_TABLE']})"
            self.last_error = f"{what}: {type(exc).__name__}: {str(exc).strip()[:300]}"

            if self._is_dead_connection_error(exc):
                # Communication failure, cancelled/timed-out call or logon
                # error: the handle is gone. Leaving ok=True (as before) made
                # every later call in the cycle hit the dead handle and wait.
                self._mark_dead(self.last_error, exc)
                return None

            if self._is_fu_not_found(exc):
                with SapSession._failures_lock:
                    SapSession._missing_load()
                    new = key not in SapSession._missing_fms
                    SapSession._missing_fms[key] = time.time()
                    SapSession._missing_save()
                if new:
                    log.info(f"[{self.system}] {name} is not installed or not remote-enabled "
                             f"({self._error_key(exc) or 'FU_NOT_FOUND'}); "
                             f"not called again for {self.MISSING_FM_RECHECK_SECONDS // 3600} h")
                return None

            if err_key in self.QUIET_KEYS:
                log.debug(f"[{self.system}] {what}: {err_key}")
                return None

            # SAP leaves message variables from an earlier call in the text,
            # so dedupe on the key, not the whole message.
            dedupe = (self.system, what, err_key or type(exc).__name__)
            with SapSession._failures_lock:
                first = dedupe not in SapSession._failures_logged
                if first:
                    SapSession._failures_logged.add(dedupe)
            if first:
                log.warning(f"[{self.system}] RFC call failed -- {self.last_error}")
            if _connection_lost(exc):
                self._dead = True      # pooled connection is gone; reconnect next time
            return None

    # RFC_READ_TABLE takes its WHERE clause as OPTIONS, a table of lines of
    # 72 characters. Anything longer is TRUNCATED BY SAP, and a clause cut
    # mid-expression raises SAPSQL_PARSE_ERROR / CX_SY_DYNAMIC_OSQL_SEMANTICS
    # in SAPLSDTX -- a short dump, on the monitored system, on every call.
    #
    # This was found the hard way: a WHERE of
    #   ( STATUS = 'A' AND ENDDATE = '...' ) OR ( STATUS = 'R' AND ... )
    # is 87 characters, and produced one dump every ten seconds on a system
    # the wall was polling.
    OPTIONS_LINE_LENGTH = 72

    @staticmethod
    def _split_where(where: str) -> list[dict]:
        """
        Break a WHERE clause into <=72-character OPTIONS lines.

        Delegates to rfc_table.split_options(), which is quote-aware: a quoted
        literal is never split across lines and the spaces inside it are kept
        (the old splitter collapsed them, so 'A  B' silently became 'A B').
        Trailing operators are pushed onto the next line, and a single token
        longer than the limit is returned whole (and logged) rather than cut.
        """
        return _rfc_table.split_options(where, SapSession.OPTIONS_LINE_LENGTH)

    # Rows per RFC_READ_TABLE round trip when a read is larger than that.
    READ_PAGE_ROWS = 5000

    def read_table_ex(self, table: str, fields: list[str], where: str = "",
                      rows: int = 500, key_fields: list[str] | None = None) -> dict:
        """
        The full rfc_table.read_table() result: rows as dicts, `partial`
        when the row cap was hit, `error` when it failed. Pages in
        READ_PAGE_ROWS steps and splits field lists wider than 512 bytes.
        """
        res = _rfc_table.read_table(self, table, fields, where, rows,
                                    key_fields=key_fields,
                                    page_size=self.READ_PAGE_ROWS)
        self.last_read = {k: res.get(k) for k in ("table", "partial", "row_cap", "chunks",
                                                  "joined_by", "error")}
        self.last_read["rows"] = len(res.get("rows") or [])
        if res.get("error") and not getattr(self, "last_error", ""):
            self.last_error = str(res["error"])
        return res

    def read_table(self, table: str, fields: list[str], where: str = "", rows: int = 500):
        """
        List of value lists in the order of `fields`, or None when the read
        failed. Values are sliced by SAP's OFFSET/LENGTH (no delimiter), so a
        "|" inside a value no longer shifts the columns. Whether the row cap
        was hit is in self.last_read["partial"].
        """
        return _rfc_table.as_lists(self.read_table_ex(table, fields, where, rows))


# --------------------------------------------------------------------------
# Function-module field mapping
# --------------------------------------------------------------------------

# EV_* export -> (canonical metric name, T-code, unit, category)
#
# NAMING IS DELIBERATE. The GUI collector emits "sap.<tcode>.<measure>" and
# the SSH collector emits bare "cpu" / "memory" / "load_1m". The RFC
# collector uses the SAME names for the SAME measurements, so that:
#
#   * the event engine, which keys on metric_name, treats a lock count read
#     over RFC and the same count read over SAP GUI as one continuous signal
#     rather than two unrelated ones;
#   * baseline history stays continuous when a system switches collection
#     path (e.g. GUI breaks and RFC takes over);
#   * correlation rules written against "sap.sm12.lock_count" fire no matter
#     which collector produced the reading.
#
# An earlier draft used human-readable labels ("ABAP Dumps (today)"), which
# would have silently forked every metric into two histories.
_FM_METRICS = {
    "EV_SHORT_DUMPS":      ("sap.st22.dumps",           "ST22",  "count", "application"),
    "EV_LOCK_ENTRIES":     ("sap.sm12.lock_count",      "SM12",  "count", "application"),
    "EV_FAILED_UPDATES":   ("sap.sm13.failed_updates",  "SM13",  "count", "application"),
    "EV_CANCELLED_JOBS":   ("sap.sm37.cancelled_jobs",  "SM37",  "count", "jobs"),
    "EV_RUNNING_JOBS":     ("sap.sm37.active_jobs",     "SM37",  "count", "jobs"),
    "EV_STUCK_TRFC":       ("sap.sm58.stuck_entries",   "SM58",  "count", "interface"),
    "EV_FAILED_IDOCS":     ("sap.we02.failed_idocs",    "WE02",  "count", "interface"),
    "EV_STUCK_OUT_QUEUES": ("sap.smq1.entries",         "SMQ1",  "count", "interface"),
    "EV_STUCK_IN_QUEUES":  ("sap.smq2.entries",         "SMQ2",  "count", "interface"),
    "EV_APP_LOG_ERRORS":   ("sap.sm21.errors",          "SM21",  "count", "application"),
    "EV_ACTIVE_USERS":     ("sap.al08.user_logons",     "AL08",  "count", "workload"),
    "EV_LOCKED_USERS":     ("sap.su01.locked_users",    "SU01",  "count", "security"),
    "EV_FREE_DIA_WP":      ("sap.sm50.free_dia_wp",     "SM50",  "count", "workload"),
    "EV_TOTAL_DIA_WP":     ("sap.sm50.total_dia_wp",    "SM50",  "count", "workload"),
    # OS metrics share the SSH collector's names on purpose -- same host,
    # same measurement. The orchestrator keeps whichever arrives first.
    # EV_CPU_UTIL_PCT, EV_MEM_UTIL_PCT, EV_TOTAL_RAM_GB and EV_LOAD_1M are
    # deliberately NOT mapped. They were fed by SM69 external commands via
    # SXPG_COMMAND_EXECUTE, which needs S_LOG_COM -- remote command execution
    # on the application server. That grant was withdrawn (abap/SXPG_REMOVAL.md)
    # and CPU/memory/load now come from /SDF/SMON_HEADER, with CCMS RZ20 as
    # the fallback. An older transported FM may still export numbers for these
    # keys; ignoring them here is what keeps a stale 99% off the dashboard.
}

# Exports where a literal 0 means the read FAILED. A live host is never at 0%
# CPU with 0 GB of RAM. load_1m is EXCLUDED — an idle system genuinely
# reports 0.00 and hiding that would discard a correct reading.
_ZERO_IS_MISSING = {"cpu", "memory", "memory.total_gb"}
# The ABAP module returns -1 for "could not read".
_NEGATIVE_IS_MISSING = {"cpu", "memory", "memory.total_gb", "load_1m"}

# TAB512 detail tables -> metric key.
_FM_TABLES = {
    "ET_SHORT_DUMPS": "sap.st22.dumps",
    "ET_LOCK_USERS": "sap.sm12.lock_count",
    "ET_CANCELLED_JOBS": "sap.sm37.cancelled_jobs",
    "ET_RUNNING_JOBS": "sap.sm37.active_jobs",
    "ET_ACTIVE_USERS": "sap.al08.user_logons",
}

# Default thresholds per metric key: (warning, critical).
# These are the fallback: config/thresholds.yaml (all systems) and
# config/thresholds_systems.yaml (one system) win -- see _limits().
_THRESHOLDS = {
    # Round 38 (company standard, 24.09.2026): 5 = WARNING, 10 = CRITICAL.
    "sap.st22.dumps": (5, 10),
    "sap.sm12.lock_count": (500, 2000),
    "sap.sm13.failed_updates": (1, 5),
    "sap.sm37.cancelled_jobs": (5, 10),
    "sap.sm58.stuck_entries": (1, 10),
    "sap.sm58.entries_today": (200, 1000),
    "sap.we02.failed_idocs": (1, 10),
    "sap.smq1.entries": (1, 5),
    "sap.smq2.entries": (1, 5),
    "sap.sm21.errors": (5, 20),
    "sap.al08.user_logons": (150, 250),
    "sap.su01.locked_users": (5, 20),
    "sap.sm66.wp_saturation_pct": (80, 90),
    "cpu": (80, 90),
    "memory": (85, 95),
}


# How many SYSFAIL rows to read from ARFCSSTATE. The old read stopped at 500,
# and PS4 hit that on every run. Override with IBO_SM58_READ_LIMIT.
SM58_READ_LIMIT_DEFAULT = 20000


def _sm58_read_limit() -> int:
    try:
        return max(int(_os.environ.get("IBO_SM58_READ_LIMIT", SM58_READ_LIMIT_DEFAULT)), 500)
    except (TypeError, ValueError):
        return SM58_READ_LIMIT_DEFAULT


def _sap_date(raw: str) -> str:
    """20260903 -> 03.09.2026"""
    raw = str(raw or "").strip()
    return f"{raw[6:8]}.{raw[4:6]}.{raw[:4]}" if len(raw) == 8 and raw.isdigit() else raw


# Round 37: the old SYSFAIL backlog (2,688 rows on PS4, oldest 2025) was read
# in full on every live poll. It changes slowly, so it is read in full at
# most every IBO_SM58_BACKLOG_TTL_SECONDS (default 30 min) per system; in
# between only TODAY's SYSFAIL rows are read -- a handful -- and merged in.
# Today's count, the one that alerts, stays live.
SM58_BACKLOG_TTL_SECONDS = int(_os.environ.get("IBO_SM58_BACKLOG_TTL_SECONDS", "1800") or 1800)
_sm58_cache: dict = {}            # system -> (monotonic time, rows, capped)
_sm58_lock = threading.Lock()


_SM58_FAILED = "( ARFCSTATE = 'SYSFAIL' OR ARFCSTATE = 'CPICERR' )"


def _sm58_rows(session, today: str, limit: int):
    """
    (rows, capped) of failed tRFC [ARFCDATUM, ARFCDEST]; (None, False) if unreadable.

    Failed means SYSFAIL or CPICERR -- both are what the SM58 screen lists as
    errors. Today's rows are always read on their own, so a backlog bigger
    than the read limit can never push today's failures out of the count.
    """
    system = getattr(session, "system", "") or ""
    now = time.monotonic()
    fresh = session.read_table("ARFCSSTATE", ["ARFCDATUM", "ARFCDEST"],
                               f"{_SM58_FAILED} AND ARFCDATUM = '{today}'", limit)
    if fresh is None:
        return None, False
    with _sm58_lock:
        hit = _sm58_cache.get(system)
    # The backlog is "everything before <day>": a cache built yesterday would
    # miss yesterday's failures, so it is only reused on the same day.
    if hit and hit[1] == today and now - hit[0] < SM58_BACKLOG_TTL_SECONDS:
        older, capped = hit[2], hit[3]
    else:
        older = session.read_table("ARFCSSTATE", ["ARFCDATUM", "ARFCDEST"],
                                   f"{_SM58_FAILED} AND ARFCDATUM < '{today}'", limit)
        if older is None:
            # Unreadable now: an earlier same-day read is better than nothing;
            # without one, say "not read" rather than "no backlog".
            if hit and hit[1] == today:
                older, capped = hit[2], hit[3]
            else:
                return None, False
        else:
            capped = len(older) >= limit
            with _sm58_lock:
                _sm58_cache[system] = (now, today, list(older), capped)
    older = [r for r in older if (str(r[0]).strip() if r else "") != today]
    return older + list(fresh), capped or len(fresh) >= limit


_SM58_STATE_WORDS = {"RECORDED": "recorded", "SYSFAIL": "system failure", "CPICERR": "communication error",
                     "EXECUTED": "executed", "SYSLOAD": "waiting (system load)", "MAILED": "mailed",
                     "READ": "read", "HOLD": "on hold", "VBRECORD": "update recorded", "NO_RESOURCES": "no resources"}


def _sm58_entries_today(session, today: str, limit: int):
    """
    sap.sm58.entries_today: SM58's "Number of Entries Displayed" (today, all users).

    ARFCSSTATE also holds every qRFC LUW waiting in an outbound queue (SMQ1:
    MCEX / NFEX / QMEX ... with destination NONE, state RECORDED) -- 15,304 of
    15,415 rows on CEAT S4P on 16 Oct, which SM58 does not list. SM58 showed
    the failed, the executing and the WORKFLOW_LOCAL "recorded" entries:
    everything except RECORDED to NONE. Failed and in-execution go in the detail,
    as SM58's Information box shows them.
    """
    from collections import Counter
    rows = session.read_table("ARFCSSTATE", ["ARFCSTATE", "ARFCDEST"], f"ARFCDATUM = '{today}'", limit)
    if rows is None:
        return None
    capped = len(rows) >= limit
    pairs = [((str(r[0]).strip() if r else "") or "?", (str(r[1]).strip() if len(r) > 1 else "") or "?")
             for r in rows]
    shown = [(s, d) for s, d in pairs if not (s == "RECORDED" and d == "NONE")]
    n = len(shown)
    states = Counter(s for s, _ in shown)
    dests = Counter(d for _, d in shown)
    failed = states.get("SYSFAIL", 0) + states.get("CPICERR", 0)
    executing = states.get("SENDED", 0) + states.get("EXECUTING", 0)
    if n:
        detail = (f"failed {failed:,}, in execution {executing:,}"
                  + "".join(f", {_SM58_STATE_WORDS.get(s, s.lower())} {c:,}" for s, c in states.most_common()
                            if s not in ("SYSFAIL", "CPICERR", "SENDED", "EXECUTING"))
                  + "; destinations: " + ", ".join(f"{d} {c:,}" for d, c in dests.most_common(3)))
    else:
        detail = "no entries in SM58 today"
    queued = len(pairs) - n
    if queued:
        detail += f" (not counted: {queued:,} qRFC entries waiting in outbound queues, see SMQ1)"
    if capped:
        detail += f"; read limit of {limit:,} reached, so at least this many"
    warn, crit = _limits("sap.sm58.entries_today")
    return MetricResult(
        name="sap.sm58.entries_today", value=float(n),
        display_value=f"{n}{'+' if capped else ''} count", status=_grade("sap.sm58.entries_today", n),
        threshold_warning=warn, threshold_critical=crit, source="rfc_collector", tcode="SM58",
        detail=detail, category="interface", unit="count",
        extra_data={"collector": "RFC_TABLE", "scope": "today", "states": dict(states), "capped": capped,
                    "failed": failed, "in_execution": executing},
    )


def _sm58_sysfail_metrics(rows, today: str, limit: int, capped: bool | None = None) -> list:
    """
    Failed (SYSFAIL) tRFC entries, split into today and older.

    sap.sm58.stuck_entries now counts TODAY's failures only -- the same scope
    as the SM58 screen's default date selection, so the two can be compared.
    It keeps its thresholds (WARNING at 1, CRITICAL at 10).

    sap.sm58.sysfail_backlog counts everything older. It is housekeeping, so
    it is graded WARNING and never CRITICAL: the old single figure mixed the
    two and put an old backlog at CRITICAL on every run.
    """
    from collections import Counter

    dates = [str(r[0]).strip() if r else "" for r in rows]
    dests = [str(r[1]).strip() if len(r) > 1 else "" for r in rows]
    capped = len(rows) >= limit if capped is None else capped
    today_count = sum(1 for d in dates if d == today)
    older = sorted(d for d in dates if d and d != today)
    top = [(d, n) for d, n in Counter(x for x in dests if x).most_common(3)]
    top_text = ", ".join(f"{d} {n:,}" for d, n in top)

    detail = "Failed tRFC entries (SYSFAIL/CPICERR) dated today (the SM58 screen's default selection)"
    if older:
        detail += (f"; {len(older):,}{'+' if capped else ''} older, oldest {_sap_date(older[0])}")
    if top_text:
        detail += f"; top destinations: {top_text}"

    warn, crit = _limits("sap.sm58.stuck_entries")
    metrics = [MetricResult(
        name="sap.sm58.stuck_entries", value=float(today_count),
        display_value=f"{today_count} count", status=_grade("sap.sm58.stuck_entries", today_count),
        threshold_warning=warn, threshold_critical=crit, source="rfc_collector", tcode="SM58",
        detail=detail, category="interface", unit="count",
        extra_data={"collector": "RFC_TABLE", "scope": "today"},
    )]
    if older:
        metrics.append(MetricResult(
            name="sap.sm58.sysfail_backlog", value=float(len(older)),
            display_value=f"{len(older)}{'+' if capped else ''} count",
            status=Status.WARNING, threshold_warning=1, threshold_critical=None,
            source="rfc_collector", tcode="SM58",
            detail=(f"Failed tRFC entries (SYSFAIL/CPICERR) older than today"
                    + (f" (read limit of {limit:,} reached, so at least this many)" if capped else "")
                    + f"; oldest {_sap_date(older[0])}"
                    + (f"; top destinations: {top_text}" if top_text else "")
                    + ". Housekeeping: review in SM58 with a wider date range, then "
                      "re-process or delete."),
            category="interface", unit="count",
            extra_data={"collector": "RFC_TABLE", "scope": "older", "capped": capped,
                        "oldest": older[0], "top_destinations": top},
        ))
    return metrics


# --------------------------------------------------------------------------
# qRFC queues -- SMQ1 (outbound, TRFCQOUT) and SMQ2 (inbound, TRFCQIN)
# --------------------------------------------------------------------------
#
# For systems without Z_GET_OBSERVABILITY_DATA. Read-only: RFC_READ_TABLE on
# the two queue tables of the logon client, nothing is changed.
#
# The value is the number of entries waiting in the queues -- what the SMQ1 /
# SMQ2 screen shows -- graded against thresholds.yaml like the screen's
# figure. Any entry in an ERROR or STOPPED state (SYSFAIL, CPICERR, STOP, ...)
# makes it at least WARNING: it blocks every entry behind it in its queue.
# BW delta queues (NOSEND) are parked on purpose and only counted.
#
# The column set differs between releases, so each table is probed once per
# process: QNAME must be readable, QSTATE / DEST are used when present.

QRFC_ERROR_STATES = {"SYSFAIL", "CPICERR", "STOP", "WAITSTOP", "ANORETRY", "ARETRY", "RETRY", "ERROR"}
QRFC_PARKED_STATES = {"NOSEND", "NOSENDS"}
QRFC_READ_LIMIT = int(_os.environ.get("IBO_QRFC_READ_LIMIT", "5000") or 5000)
QRFC_TTL_SECONDS = int(_os.environ.get("IBO_QRFC_TTL_SECONDS", "120") or 120)
_QRFC = {
    "SMQ1": ("TRFCQOUT", "sap.smq1.entries", "outbound"),
    "SMQ2": ("TRFCQIN", "sap.smq2.entries", "inbound"),
}
_qrfc_fields_cache: dict = {}     # (system, table) -> [optional fields] | None (table unreadable)
_qrfc_rows_cache: dict = {}       # (system, table) -> (monotonic, rows, fields)
_qrfc_lock = threading.Lock()


def _qrfc_transient(text: str) -> bool:
    t = (text or "").upper()
    return any(w in t for w in ("COMMUNICATION", "TIMEOUT", "CLOSED", "CANCEL", "NO RFC SESSION"))


def _qrfc_fields(session, table: str):
    """Optional columns this system has for `table`; None if the table is not readable."""
    system = getattr(session, "system", "") or ""
    key = (system, table)
    with _qrfc_lock:
        if key in _qrfc_fields_cache:
            return _qrfc_fields_cache[key]
    if session.read_table(table, ["QNAME"], "", 1) is None:
        err = str(getattr(session, "last_error", "") or "")
        if not _qrfc_transient(err):
            with _qrfc_lock:
                _qrfc_fields_cache[key] = None
        return None
    ok = []
    for field in ("QSTATE", "DEST"):
        if session.read_table(table, ["QNAME", field], "", 1) is not None:
            ok.append(field)
    with _qrfc_lock:
        _qrfc_fields_cache[key] = ok
    return ok


def _qrfc_rows(session, table: str):
    """(rows, fields) -- rows as dicts; (None, reason) when unreadable. Cached QRFC_TTL_SECONDS."""
    system = getattr(session, "system", "") or ""
    key = (system, table)
    now = time.monotonic()
    with _qrfc_lock:
        hit = _qrfc_rows_cache.get(key)
    if hit and now - hit[0] < QRFC_TTL_SECONDS:
        return hit[1], hit[2]
    fields = _qrfc_fields(session, table)
    if fields is None:
        return None, str(getattr(session, "last_error", "") or "table not readable")[:200]
    cols = ["QNAME"] + fields
    raw = session.read_table(table, cols, "", QRFC_READ_LIMIT)
    if raw is None:
        return None, str(getattr(session, "last_error", "") or "read failed")[:200]
    rows = [{c: (r[i].strip() if i < len(r) else "") for i, c in enumerate(cols)} for r in raw]
    with _qrfc_lock:
        _qrfc_rows_cache[key] = (now, rows, fields)
    return rows, fields


def _qrfc_metric(tcode: str, rows, fields) -> MetricResult:
    """One SMQ1 / SMQ2 metric from the queue rows (see the section note)."""
    from collections import Counter, defaultdict
    table, name, direction = _QRFC[tcode]
    warn, crit = _limits(name)
    if rows is None:
        return MetricResult(
            name=name, value=None, display_value="Not measured", status=Status.UNKNOWN,
            threshold_warning=warn, threshold_critical=crit, source="rfc_collector", tcode=tcode,
            detail=f"{table} not readable: {fields}"[:300], category="interface", unit="count",
            extra_data={"collector": "RFC_TABLE", "read_failed": True})
    capped = len(rows) >= QRFC_READ_LIMIT
    plus = "+" if capped else ""
    has_state = "QSTATE" in fields
    # NOSEND queues hold BW delta data until BW pulls it -- parked on
    # purpose, often thousands of entries. Counted in the detail, not graded.
    parked = [r for r in rows if has_state and (r.get("QSTATE") or "").upper() in QRFC_PARKED_STATES]
    live = ([r for r in rows if (r.get("QSTATE") or "").upper() not in QRFC_PARKED_STATES]
            if parked else rows)
    total = len(live)
    queues = Counter(r["QNAME"] for r in live if r.get("QNAME"))
    states = Counter(r.get("QSTATE", "") or "?" for r in rows) if has_state else Counter()
    bad = [r for r in live if has_state and (r.get("QSTATE") or "").upper() in QRFC_ERROR_STATES]
    bad_q: dict = defaultdict(lambda: {"n": 0, "state": "", "dest": ""})
    for r in bad:
        q = bad_q[r["QNAME"]]
        q["n"] += 1
        q["state"] = q["state"] or r.get("QSTATE", "")
        q["dest"] = q["dest"] or r.get("DEST", "")
    top = sorted(bad_q.items(), key=lambda kv: -kv[1]["n"])[:5]
    # Graded the same way as the SMQ1 / SMQ2 screen: entries waiting in the
    # queues against thresholds.yaml. On top, any entry in an error or
    # stopped state makes it at least WARNING -- it blocks its whole queue.
    status = _grade(name, total)
    if bad and status == Status.NORMAL:
        status = Status.WARNING
    summary = f"{total:,}{plus} entries in {len(queues):,} {direction} queue(s)"
    if has_state and states:
        summary += " (" + ", ".join(f"{k} {v:,}" for k, v in states.most_common(5)) + ")"
    if parked:
        summary += f"; {len(parked):,} in NOSEND (BW delta, not graded)"
    if top:
        summary = (f"{len(bad):,} in error/stopped in {len(bad_q):,} queue(s): "
                   + "; ".join(f"{qn} {q['state']}{(' -> ' + q['dest']) if q['dest'] else ''} ({q['n']})"
                               for qn, q in top)
                   + f". {summary}")
    if not has_state:
        summary += "; queue states not readable on this release (errors cannot be told apart)"
    return MetricResult(
        name=name, value=float(total), display_value=f"{total}{plus} count", status=status,
        threshold_warning=warn, threshold_critical=crit, source="rfc_collector", tcode=tcode,
        detail=summary[:600], category="interface", unit="count",
        extra_data={"collector": "RFC_TABLE", "total_entries": total, "queues": len(queues),
                    "error_entries": len(bad) if has_state else None, "parked_entries": len(parked),
                    "capped": capped, "states": dict(states),
                    "error_queues": [{"queue": qn, **q} for qn, q in top]})


def _qrfc_metrics(session) -> list:
    out = []
    for tcode, (table, _name, _dir) in _QRFC.items():
        try:
            rows, fields = _qrfc_rows(session, table)
            out.append(_qrfc_metric(tcode, rows, fields))
        except Exception as exc:  # noqa: BLE001 -- one table must not cost the other checks
            log.warning(f"{getattr(session, 'system', '?')}: {tcode} read failed: {exc}")
    return out


def _as_number(raw):
    try:
        text = str(raw).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _limits(key: str) -> tuple:
    """(warning, critical) for the system being read: its override, else thresholds.yaml, else the default."""
    default = _THRESHOLDS.get(key, (None, None))
    try:
        from core import thresholds as _thr
        return _thr.limits(key, default)
    except Exception:  # noqa: BLE001 -- a config problem must not stop the read
        return default


def _for_session_system(fn):
    """Grade inside `fn(session, ...)` with that session's system's thresholds."""
    import functools

    @functools.wraps(fn)
    def wrapper(session, *args, **kwargs):
        from core import thresholds as _thr
        with _thr.use_system(getattr(session, "system", "") or _thr.current_system()):
            return fn(session, *args, **kwargs)
    return wrapper


def _switched_off(key: str) -> bool:
    """True when the Thresholds page switched this metric off (for the current system)."""
    try:
        from core import thresholds as _thr
        return _thr.spec(key).get("enabled") is False
    except Exception:  # noqa: BLE001
        return False


def _grade(key: str, value: float | None) -> Status:
    if value is None:
        return Status.UNKNOWN
    if _switched_off(key):
        return Status.NORMAL
    warn, crit = _limits(key)
    try:
        # "Limit is critical" on the Thresholds page; not set = the critical
        # number itself is critical, as this collector always graded.
        from core import thresholds as _thr
        incl = _thr.inclusive(key, default=True)
    except Exception:  # noqa: BLE001
        incl = True
    if crit is not None and (value >= crit if incl else value > crit):
        return Status.CRITICAL
    if warn is not None and value >= warn:
        return Status.WARNING
    return Status.NORMAL


def _detail_rows(entries, key) -> str:
    """TAB512 rows -> a short human-readable detail string."""
    out = []
    for e in entries or []:
        text = str(e.get("WA", "") if isinstance(e, dict) else e).strip()
        if not text:
            continue
        if key == "sap.st22.dumps" and "|" in text:
            parts = [p.strip() for p in text.split("|")]
            out.append(f"{parts[0]} {parts[1] if len(parts) > 1 else ''}".strip())
        else:
            out.append(text)
    return ", ".join(out[:10])


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

@_for_session_system
def _from_function_module(session: SapSession) -> list[MetricResult]:
    result = session.call("Z_GET_OBSERVABILITY_DATA")
    if not result:
        return []

    details = {key: _detail_rows(result.get(param), key)
               for param, key in _FM_TABLES.items()}

    metrics: list[MetricResult] = []
    for export, (name, tcode, unit, category) in _FM_METRICS.items():
        if export not in result:
            continue
        value = _as_number(result[export])
        if value is not None:
            if name in _NEGATIVE_IS_MISSING and value < 0:
                value = None
            elif name in _ZERO_IS_MISSING and value == 0:
                value = None

        status = _grade(name, value)
        if (name in ("sap.smq1.entries", "sap.smq2.entries") and value
                and status == Status.NORMAL):
            # The Z module counts only queue entries in SYSFAIL / CPICERR:
            # any of them blocks its queue (same rule as the table read).
            status = Status.WARNING
        if value is None:
            display = "No data"
        elif unit == "%":
            display = f"{value:.0f}%"
        elif unit == "GB":
            display = f"{value:.0f} GB"
        elif unit == "":
            display = f"{value:.2f}"
        else:
            display = f"{int(value)} count"

        warn, crit = _limits(name)
        metrics.append(MetricResult(
            name=name,
            value=value,
            display_value=display,
            status=status,
            threshold_warning=warn,
            threshold_critical=crit,
            source="rfc_collector",
            tcode=tcode,
            detail=details.get(name, ""),
            category=category,
            unit=unit,
            metric_type="gauge",
            extra_data={"collector": "Z_FM"},
        ))

    # Work-process saturation needs both halves, so derive it here.
    free = next((m.value for m in metrics if m.name == "sap.sm50.free_dia_wp"), None)
    total = next((m.value for m in metrics if m.name == "sap.sm50.total_dia_wp"), None)
    if free is not None and total:
        pct = round((total - free) / total * 100)
        metrics.append(MetricResult(
            name="sap.sm66.wp_saturation_pct", value=float(pct), display_value=f"{pct}%",
            status=_grade("sap.sm66.wp_saturation_pct", pct),
            threshold_warning=80, threshold_critical=90,
            source="rfc_collector", tcode="SM66", category="workload", unit="%",
            extra_data={"collector": "Z_FM"},
        ))

    backup = str(result.get("EV_LAST_BACKUP", "") or "").strip()
    if backup:
        known = backup not in ("N/A", "")
        metrics.append(MetricResult(
            name="sap.db12.last_backup",
            value=None,
            display_value=backup if known else "No data",
            status=Status.NORMAL if known else Status.UNKNOWN,
            source="rfc_collector", tcode="DB12", category="database",
            metric_type="state",
            extra_data={"collector": "Z_FM"},
        ))

    return metrics


# Upper bound on SNAP header rows (one per dump) read to COUNT a day's dumps.
# Was 2000, which silently capped a real day of 3,480 dumps. The read pages
# in SapSession.READ_PAGE_ROWS steps (one UZEIT column, ~6 chars a row), and
# a count that still reaches this bound is reported with partial=True.
def _snap_count_limit() -> int:
    try:
        return max(int(_os.environ.get("IBO_SNAP_COUNT_LIMIT", "50000")), 100)
    except (TypeError, ValueError):
        return 50000


_SNAP_READ_LIMIT = _snap_count_limit()


def _last_read_partial(session, rows, limit) -> bool:
    """Did the last read_table() stop at its row cap? Falls back to len >= limit."""
    meta = getattr(session, "last_read", None)
    if isinstance(meta, dict) and "partial" in meta:
        return bool(meta.get("partial"))
    return rows is not None and len(rows) >= limit

# --------------------------------------------------------------------------
# Round 33: ABAP dumps from /SDF/GET_DUMP_LOG (ST-PI)
# --------------------------------------------------------------------------
#
# RFC_READ_TABLE answers TABLE_NOT_AVAILABLE for SNAP on CARFOUR QAS and PS4,
# so the live wall had no dump count there. /SDF/GET_DUMP_LOG is SAP's own
# remote-enabled dump reader (function group /SDF/SMD_E2E, used by Solution
# Manager). Checked on 23.09.2026 against ST22: CAQ 1 = 1, PS4 18 = 18, same
# times, users and programs.
#
# ET_E2E_LOG (/SDF/E2E_LOG_STRUC), as filled on both systems:
#   E2E_DATE, E2E_TIME  system date and time (match ST22's columns)
#   E2E_USER            user and client joined: "58128_500", "IB_SATYA_800"
#   E2E_HOST            instance, e.g. sapxps4ci_PS4_00
#   FIELD1 runtime error   FIELD2 exception class   FIELD3 app. component
#   FIELD4 program
# A day without dumps raises NO_DATA_FOUND -- that is 0 dumps, not a failure.

DUMP_LOG_FM = "/SDF/GET_DUMP_LOG"

# Systems where the FM does not exist. Asked once per process, not every poll.
_dump_log_missing: set = set()
_dump_log_lock = threading.Lock()


def _split_user_client(value: str) -> tuple[str, str]:
    """"58128_500" -> ("58128", "500"); a name with no client suffix is kept whole."""
    value = str(value or "").strip()
    head, sep, tail = value.rpartition("_")
    if sep and head and len(tail) == 3 and tail.isdigit():
        return head, tail
    return value, ""


def read_dump_log(session, day: str, time_from: str = "000000", time_to: str = "235959"):
    """
    One day's dumps (optionally a time range within it, SAP server time)
    via /SDF/GET_DUMP_LOG, as a list of dicts:
    {date, time, user, client, host, error, exception, component, program}.

    Returns [] for a day without dumps and None when the FM cannot be used
    (not installed, not authorised, or the call failed) -- the caller then
    falls back to SNAP. The reason is left in session.last_error.
    """
    system = getattr(session, "system", "") or ""
    with _dump_log_lock:
        if system in _dump_log_missing:
            return None
    res = session.call(DUMP_LOG_FM, DATE_FROM=day, TIME_FROM=time_from,
                       DATE_TO=day, TIME_TO=time_to)
    if res is None:
        why = str(getattr(session, "last_error", "") or "")
        if "NO_DATA_FOUND" in why:
            return []
        if "FU_NOT_FOUND" in why:
            with _dump_log_lock:
                _dump_log_missing.add(system)
        return None

    def s(row, key):
        return str(row.get(key, "") or "").strip()

    rows = []
    for r in res.get("ET_E2E_LOG", []) or []:
        user, client = _split_user_client(s(r, "E2E_USER"))
        rows.append({
            "date": s(r, "E2E_DATE"), "time": s(r, "E2E_TIME"),
            "user": user, "client": client, "host": s(r, "E2E_HOST"),
            "error": s(r, "FIELD1"), "exception": s(r, "FIELD2"),
            "component": s(r, "FIELD3"), "program": s(r, "FIELD4"),
        })
    return rows


def _top(values, n=3) -> str:
    from collections import Counter
    counts = Counter(v for v in values if v)
    return ", ".join(f"{k} {v}" for k, v in counts.most_common(n))


def _st22_from_snap(session, today: str, metrics: list) -> None:
    """
    Today's ABAP dumps from SNAP, appended to `metrics` as sap.st22.dumps.

    Round 32, two fixes:

    * COUNT DUMPS, NOT ROWS. SNAP stores each dump as several rows -- SEQNO
      000 is the first, the rest carry the dump data. Counting every row for
      today overstated the dump count. The read now asks for SEQNO '000'.
      Some kernels number differently; if that finds nothing but today has
      SNAP rows at all, the rows are read and de-duplicated on (time, host,
      work process), the same rule the dump breakdown uses.

    * A FAILED READ IS SHOWN, NOT DROPPED. It used to add nothing, so the
      check disappeared from the wall. It is now UNKNOWN ("Not measured")
      with SAP's reason in the detail, which the wall prints under the check.
    """
    # Round 33: SAP's dump reader first; SNAP only where it is unavailable.
    warn, crit = _limits("sap.st22.dumps")
    log_rows = read_dump_log(session, today)
    if log_rows is not None:
        top_errors = _top(r["error"] for r in log_rows)
        metrics.append(MetricResult(
            name="sap.st22.dumps", value=float(len(log_rows)),
            display_value=f"{len(log_rows)} count",
            status=_grade("sap.st22.dumps", len(log_rows)),
            threshold_warning=warn, threshold_critical=crit,
            source="rfc_collector", tcode="ST22",
            detail=(f"{top_errors} -- via {DUMP_LOG_FM}" if top_errors
                    else f"no dumps today -- via {DUMP_LOG_FM}"),
            category="application", unit="count",
            extra_data={"collector": "RFC_TABLE", "dump_source": DUMP_LOG_FM,
                        "top_errors": top_errors,
                        "top_programs": _top(r["program"] for r in log_rows)},
        ))
        return
    fm_error = str(getattr(session, "last_error", "") or "")

    rows = session.read_table("SNAP", ["UZEIT"],
                              f"DATUM = '{today}' AND SEQNO = '000'", _SNAP_READ_LIMIT)
    capped = _last_read_partial(session, rows, _SNAP_READ_LIMIT)
    note = "one row per dump (SEQNO 000)"
    if rows is not None and not rows:
        # Nothing numbered 000 -- a quiet day, or a kernel that numbers
        # differently. One row answers which.
        probe = session.read_table("SNAP", ["DATUM"], f"DATUM = '{today}'", 1)
        if probe:
            wide_limit = _SNAP_READ_LIMIT * 2
            wide = session.read_table("SNAP", ["UZEIT", "AHOST", "MODNO"],
                                      f"DATUM = '{today}'", wide_limit)
            if wide is not None:
                capped = _last_read_partial(session, wide, wide_limit)
                rows = list({tuple(c.strip() for c in r) for r in wide})
                note = "de-duplicated on time, host and work process"
            else:
                rows = None

    if rows is None:
        reason = str(getattr(session, "last_error", "") or "").strip() or "no reason returned"
        if fm_error and "FU_NOT_FOUND" not in fm_error:
            reason += f"; {DUMP_LOG_FM}: {fm_error}"
        metrics.append(MetricResult(
            name="sap.st22.dumps", value=None, display_value="Not measured",
            status=Status.UNKNOWN, threshold_warning=warn, threshold_critical=crit,
            source="rfc_collector", tcode="ST22",
            detail=f"SNAP not readable: {reason}"[:300],
            category="application", unit="count",
            extra_data={"collector": "RFC_TABLE", "read_failed": True},
        ))
        return

    count = len(rows)
    metrics.append(MetricResult(
        name="sap.st22.dumps", value=float(count),
        display_value=f"{count}+ count" if capped else f"{count} count",
        status=_grade("sap.st22.dumps", count), threshold_warning=warn, threshold_critical=crit,
        source="rfc_collector", tcode="ST22",
        detail=note + (f"; read limit {_SNAP_READ_LIMIT:,} reached, so at least that many"
                       if capped else ""),
        category="application", unit="count",
        extra_data={"collector": "RFC_TABLE", "partial": capped, "day": today,
                    "row_cap": _SNAP_READ_LIMIT if capped else None},
    ))


def dumps_by_user_window(system: str, minutes: int = 15, session=None,
                         cfg: dict | None = None) -> dict[str, int] | None:
    """
    ABAP dumps per SAP user in the last `minutes` minutes of SAP SERVER time.

    For the alert rule "more than 10 dumps by one user within 15 minutes".
    Returns {user: count} ({} when there were none), or None when the dumps
    could not be read -- None is "unknown", never "no dumps".

    Uses an open `session` when given (no extra logon); otherwise opens one
    RFC logon for `system` (cfg from config/systems.yaml unless passed),
    through the logon guard like every other path.

    Source: /SDF/GET_DUMP_LOG with a time range (one row per dump), else
    SNAP header rows (SEQNO '000'). A window crossing SAP midnight reads
    both days.
    """
    minutes = max(1, int(minutes))
    if session is None:
        if cfg is None:
            try:
                from core.config_loader import get_systems
                cfg = next((c for c in get_systems() if c.get("name") == system), None)
            except Exception:  # noqa: BLE001
                cfg = None
        if not cfg:
            return None
        if cooldown_remaining(system)[0]:
            return None
        with SapSession(system, cfg) as s:
            if not s.ok:
                return None
            return dumps_by_user_window(system, minutes, session=s)

    _sap_clock.ensure_offset(session, system)
    end = _sap_clock.sap_now(system)
    start = end - timedelta(minutes=minutes)
    spans = []
    if start.date() == end.date():
        spans.append((start.strftime("%Y%m%d"), start.strftime("%H%M%S"), end.strftime("%H%M%S")))
    else:
        spans.append((start.strftime("%Y%m%d"), start.strftime("%H%M%S"), "235959"))
        spans.append((end.strftime("%Y%m%d"), "000000", end.strftime("%H%M%S")))

    counts: dict[str, int] = {}
    log_ok = True
    for day, t_from, t_to in spans:
        rows = read_dump_log(session, day, t_from, t_to)
        if rows is None:
            log_ok = False
            break
        for r in rows:
            t = str(r.get("time") or "").replace(":", "")
            if t and not (t_from <= t[:6] <= t_to):
                continue        # the FM may round the range; keep the window exact
            user = r.get("user") or "?"
            counts[user] = counts.get(user, 0) + 1
    if log_ok:
        return counts

    counts = {}
    for day, t_from, t_to in spans:
        where = (f"DATUM = '{day}' AND UZEIT >= '{t_from}' AND UZEIT <= '{t_to}'"
                 f" AND SEQNO = '000'")
        rows = session.read_table("SNAP", ["UNAME"], where, _SNAP_READ_LIMIT)
        if rows is None:
            return None
        for r in rows:
            user = (r[0] if r else "").strip() or "?"
            counts[user] = counts.get(user, 0) + 1
    return counts


@_for_session_system
def _from_standard_modules(session: SapSession) -> list[MetricResult]:
    """
    Fallback for systems without Z_GET_OBSERVABILITY_DATA.
    Only emits metrics it could actually read — never a placeholder zero.
    """
    # SAP server date, not the VM's: SNAP, TBTCO and ARFCSSTATE are dated in
    # SAP time, and around midnight the two differ.
    _sap_clock.ensure_offset(session, getattr(session, "system", "") or "")
    today = _sap_clock.sap_today(getattr(session, "system", "") or "")
    metrics: list[MetricResult] = []

    def add(name, tcode, value, unit="count", category="application", detail=""):
        if value is None:
            return
        warn, crit = _limits(name)
        metrics.append(MetricResult(
            name=name, value=float(value), display_value=f"{int(value)} count",
            status=_grade(name, value), threshold_warning=warn, threshold_critical=crit,
            source="rfc_collector", tcode=tcode, detail=detail,
            category=category, unit=unit,
            extra_data={"collector": "RFC_TABLE"},
        ))

    _st22_from_snap(session, today, metrics)

    cancelled = session.read_table("TBTCO", ["JOBNAME"], f"STATUS = 'A' AND SDLSTRTDT = '{today}'", 5000)
    add("sap.sm37.cancelled_jobs", "SM37", None if cancelled is None else len(cancelled),
        category="jobs",
        detail="" if not cancelled else ", ".join(r[0].strip() for r in cancelled[:8]))
    if metrics and metrics[-1].name == "sap.sm37.cancelled_jobs":
        # TBTCO is read for jobs scheduled TODAY; the SM37 screen covers
        # since yesterday, so the screen legitimately sees more.
        metrics[-1].extra_data = {**(metrics[-1].extra_data or {}), "scope": "today"}

    locks = session.call("ENQUE_READ2", GCLIENT=str(session.cfg.get("client", "000")), GUNAME="")
    if locks is not None:
        enq = locks.get("ENQ", [])
        add("sap.sm12.lock_count", "SM12", len(enq),
            detail=", ".join(str(e.get("GUNAME", "")).strip() for e in enq[:8]))

    # Failed tRFC, split into today (comparable with the SM58 screen) and the
    # older backlog. Date and destination are read so the report can say how
    # old the backlog is and where it points.
    limit = _sm58_read_limit()
    trfc, capped = _sm58_rows(session, today, limit)
    if trfc is not None:
        metrics.extend(_sm58_sysfail_metrics(trfc, today, limit, capped=capped))
    else:
        # Field list rejected on this release: fall back to the old count,
        # all dates, and say what it is.
        trfc = session.read_table("ARFCSSTATE", ["ARFCIPID"], _SM58_FAILED, 500)
        trfc_capped = trfc is not None and len(trfc) >= 500
        add("sap.sm58.stuck_entries", "SM58", None if trfc is None else len(trfc),
            detail=("Failed tRFC entries (SYSFAIL/CPICERR), all dates"
                    + ("; read limit of 500 reached, so at least 500" if trfc_capped else "")),
            category="interface")

    # Every tRFC entry of today, any state -- SM58's "Number of entries
    # displayed" with its default selection (today, all users).
    m = _sm58_entries_today(session, today, limit)
    if m is not None:
        metrics.append(m)

    # qRFC queues (SMQ1 outbound, SMQ2 inbound).
    metrics.extend(_qrfc_metrics(session))

    users = session.call("TH_USER_LIST")
    if users is not None:
        entries = users.get("LIST", [])
        add("sap.al08.user_logons", "AL08", len(entries), category="workload",
            detail=", ".join(str(u.get("BNAME", "")).strip() for u in entries[:8]))

    wp = session.call("TH_WPINFO")
    if wp is not None:
        wps = wp.get("WPLIST", [])
        dia = [w for w in wps if str(w.get("WP_TYP", "")).strip().upper() == "DIA"]
        if dia:
            busy = [w for w in dia if not str(w.get("WP_STATUS", "")).strip().lower().startswith("wait")]
            pct = round(len(busy) / len(dia) * 100)
            metrics.append(MetricResult(
                name="sap.sm66.wp_saturation_pct", value=float(pct), display_value=f"{pct}%",
                status=_grade("sap.sm66.wp_saturation_pct", pct),
                threshold_warning=80, threshold_critical=90,
                source="rfc_collector", tcode="SM66", category="workload", unit="%",
                extra_data={"collector": "RFC_TABLE"},
            ))

    return metrics


def collect_rfc_metrics(system: str, cfg: dict) -> tuple[list[MetricResult], str | None]:
    """
    Opens ONE RFC connection and returns (metrics, error).

    An empty list with an error string means the system is UNKNOWN, not
    healthy. Callers must not treat that as "nothing wrong".
    """
    remaining, cached = cooldown_remaining(system)
    if remaining:
        return [], f"{cached} (retrying in {remaining}s)"

    with SapSession(system, cfg) as session:
        if not session.ok:
            # A guard block is persistent and shared; parking it as well
            # would only add a misleading "retrying in N s" to the message.
            if not session.logon_blocked:
                _park(system, session.error or "")
            return [], session.error

        clear_cooldown(system)
        metrics = _from_function_module(session)
        if not metrics:
            metrics = _from_standard_modules(session)
        if not metrics:
            return [], "connected, but no metrics could be read"
        return metrics, None

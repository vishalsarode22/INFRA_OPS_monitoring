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
from datetime import datetime

from core.models import MetricResult, Status
from utils.logger import get_logger, LOG_DIR as _LOG_DIR

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

_cooldowns: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _cooldown_for(error_text: str) -> int:
    low = (error_text or "").lower()
    if any(t in low for t in ("timed out", "wsaetimedout", "not reached", "refused")):
        return _COOLDOWN_TIMEOUT
    if "route permission denied" in low or "saprouter" in low:
        return _COOLDOWN_ROUTE
    if any(t in low for t in ("logon", "password", "locked", "not authorized")):
        return _COOLDOWN_AUTH
    return _COOLDOWN_DEFAULT


def cooldown_remaining(system: str) -> tuple[int, str | None]:
    with _lock:
        entry = _cooldowns.get(system)
    if not entry:
        return 0, None
    until, error = entry
    left = until - time.monotonic()
    if left <= 0:
        with _lock:
            _cooldowns.pop(system, None)
        return 0, None
    return int(left), error


def _park(system: str, error_text: str) -> None:
    seconds = _cooldown_for(error_text)
    with _lock:
        _cooldowns[system] = (time.monotonic() + seconds, error_text)


def clear_cooldown(system: str | None = None) -> None:
    """Retry immediately — call after credentials change."""
    with _lock:
        _cooldowns.clear() if system is None else _cooldowns.pop(system, None)


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

    # Bound the connect attempt. Without these the NetWeaver RFC library
    # inherits the OS TCP timeout -- about 21s on Windows -- so a host that
    # is powered off or firewalled holds a worker for that whole time. Both
    # are standard SAP connection parameters, passed straight through by
    # pyrfc, and both are overridable per system in systems.yaml.
    params["CPIC_MAX_CONV"] = "50"
    for key, env, default in (
        ("NWRFC_CONNECT_TIMEOUT", "IBO_RFC_CONNECT_TIMEOUT", "8"),
        ("NWRFC_COMM_TIMEOUT", "IBO_RFC_COMM_TIMEOUT", "30"),
    ):
        value = str(rfc.get(key.lower()) or _os.environ.get(env) or default).strip()
        if value and value != "0":
            params[key] = value
    return params


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

    timeout = float(_os.environ.get("IBO_RFC_TCP_PROBE_TIMEOUT", "2") or 2)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, (f"host {host}:{port} not reachable "
                       f"({exc.__class__.__name__}) -- network/Basis check, "
                       f"not a code issue")


class SapSession:
    """One RFC logon per cycle. `ok` is False when the system is unreachable."""

    def __init__(self, system: str, cfg: dict):
        self.system = system
        self.cfg = cfg or {}
        self.conn = None
        self.ok = False
        self.error: str | None = None
        # Round 32: the reason the most recent call() failed, "" when it did
        # not. call() still never raises; this is how a caller finds out WHY.
        self.last_error: str = ""

    def __enter__(self):
        if not PYRFC_AVAILABLE:
            self.error = "pyrfc not installed (NetWeaver RFC SDK required)"
            return self
        params = build_rfc_params(self.cfg)
        if not params:
            self.error = "no RFC configuration (ashost, username or password missing)"
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

        try:
            self.conn = Connection(**params)
            self.ok = True
        except Exception as exc:
            self.error = str(exc)
        return self

    def __exit__(self, *exc):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        return False

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
        return cls._error_key(exc) == "FU_NOT_FOUND" or "FU_NOT_FOUND" in str(exc)

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

            if self._is_fu_not_found(exc):
                with SapSession._failures_lock:
                    SapSession._missing_load()
                    new = key not in SapSession._missing_fms
                    SapSession._missing_fms[key] = time.time()
                    SapSession._missing_save()
                if new:
                    log.info(f"[{self.system}] {name} is not installed (FU_NOT_FOUND); "
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
        Break a WHERE clause into <=72-character lines.

        SAP concatenates the lines back together, so the split must fall on a
        SPACE. Splitting mid-token would join two lines into one broken
        identifier and produce the same parse error this exists to prevent.

        A single token longer than the limit cannot be split safely -- that is
        returned whole and will fail, loudly, rather than being silently cut
        into something that parses into a different query.
        """
        where = " ".join(str(where or "").split())
        if not where:
            return []

        lines, current = [], ""
        for token in where.split(" "):
            if not current:
                current = token
            elif len(current) + 1 + len(token) <= SapSession.OPTIONS_LINE_LENGTH:
                current = f"{current} {token}"
            else:
                lines.append(current)
                current = token
        if current:
            lines.append(current)

        # A line must never END on a dangling operator. How SAP rejoins the
        # lines -- with a space or without -- is not something to rely on, and
        # "... AND" + "FIELD = 'x'" concatenated without a space becomes
        # ANDFIELD and parses as nothing. Trailing operators are pushed onto
        # the next line instead.
        fixed: list[str] = []
        carry = ""
        for line in lines:
            line = (carry + " " + line).strip() if carry else line
            carry = ""
            parts = line.split(" ")
            while parts and parts[-1].upper() in ("AND", "OR", "NOT", "(", "="):
                carry = (parts.pop() + " " + carry).strip()
            if parts:
                fixed.append(" ".join(parts))
            elif carry:
                # Whole line was operators; keep it rather than drop a clause.
                fixed.append(carry)
                carry = ""
        if carry:
            fixed.append(carry)

        if any(len(line) > SapSession.OPTIONS_LINE_LENGTH for line in fixed):
            # Caller's clause cannot be expressed safely. Better to say so
            # than to send something SAP will truncate into a short dump.
            log.warning("WHERE clause cannot be split within "
                        f"{SapSession.OPTIONS_LINE_LENGTH} chars without "
                        f"breaking a token: {where[:120]}")

        return [{"TEXT": line} for line in fixed]

    def read_table(self, table: str, fields: list[str], where: str = "", rows: int = 500):
        res = self.call(
            "RFC_READ_TABLE",
            QUERY_TABLE=table,
            DELIMITER="|",
            FIELDS=[{"FIELDNAME": f} for f in fields],
            OPTIONS=self._split_where(where),
            ROWCOUNT=rows,
        )
        return None if res is None else [r["WA"].split("|") for r in res.get("DATA", [])]


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
# Tune in config/thresholds.yaml; these are the fallback.
_THRESHOLDS = {
    "sap.st22.dumps": (1, 5),
    "sap.sm12.lock_count": (500, 2000),
    "sap.sm13.failed_updates": (1, 5),
    "sap.sm37.cancelled_jobs": (1, 5),
    "sap.sm58.stuck_entries": (1, 10),
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


def _sm58_sysfail_metrics(rows, today: str, limit: int) -> list:
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
    capped = len(rows) >= limit
    today_count = sum(1 for d in dates if d == today)
    older = sorted(d for d in dates if d and d != today)
    top = [(d, n) for d, n in Counter(x for x in dests if x).most_common(3)]
    top_text = ", ".join(f"{d} {n:,}" for d, n in top)

    detail = "SYSFAIL tRFC entries dated today (the SM58 screen's default selection)"
    if older:
        detail += (f"; {len(older):,}{'+' if capped else ''} older, oldest {_sap_date(older[0])}")
    if top_text:
        detail += f"; top destinations: {top_text}"

    warn, crit = _THRESHOLDS["sap.sm58.stuck_entries"]
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
            detail=(f"SYSFAIL tRFC entries older than today"
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


def _as_number(raw):
    try:
        text = str(raw).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _grade(key: str, value: float | None) -> Status:
    if value is None:
        return Status.UNKNOWN
    warn, crit = _THRESHOLDS.get(key, (None, None))
    if crit is not None and value >= crit:
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

        warn, crit = _THRESHOLDS.get(name, (None, None))
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


_SNAP_READ_LIMIT = 2000

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
#   E2E_HOST            instance, e.g. vhrrnps4ci_PS4_00
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


def read_dump_log(session, day: str):
    """
    Today's dumps via /SDF/GET_DUMP_LOG, as a list of dicts:
    {date, time, user, client, host, error, exception, component, program}.

    Returns [] for a day without dumps and None when the FM cannot be used
    (not installed, not authorised, or the call failed) -- the caller then
    falls back to SNAP. The reason is left in session.last_error.
    """
    system = getattr(session, "system", "") or ""
    with _dump_log_lock:
        if system in _dump_log_missing:
            return None
    res = session.call(DUMP_LOG_FM, DATE_FROM=day, TIME_FROM="000000",
                       DATE_TO=day, TIME_TO="235959")
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
    warn, crit = _THRESHOLDS.get("sap.st22.dumps", (None, None))
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

    rows = session.read_table("SNAP", ["DATUM"],
                              f"DATUM = '{today}' AND SEQNO = '000'", _SNAP_READ_LIMIT)
    note = "one row per dump (SEQNO 000)"
    if rows is not None and not rows:
        # Nothing numbered 000 -- a quiet day, or a kernel that numbers
        # differently. One row answers which.
        probe = session.read_table("SNAP", ["DATUM"], f"DATUM = '{today}'", 1)
        if probe:
            wide = session.read_table("SNAP", ["UZEIT", "AHOST", "MODNO"],
                                      f"DATUM = '{today}'", _SNAP_READ_LIMIT * 5)
            if wide is not None:
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
    capped = count >= _SNAP_READ_LIMIT
    metrics.append(MetricResult(
        name="sap.st22.dumps", value=float(count),
        display_value=f"{count}+ count" if capped else f"{count} count",
        status=_grade("sap.st22.dumps", count), threshold_warning=warn, threshold_critical=crit,
        source="rfc_collector", tcode="ST22",
        detail=note + (f"; read limit {_SNAP_READ_LIMIT} reached, so at least that many" if capped else ""),
        category="application", unit="count",
        extra_data={"collector": "RFC_TABLE"},
    ))


def _from_standard_modules(session: SapSession) -> list[MetricResult]:
    """
    Fallback for systems without Z_GET_OBSERVABILITY_DATA.
    Only emits metrics it could actually read — never a placeholder zero.
    """
    today = datetime.now().strftime("%Y%m%d")
    metrics: list[MetricResult] = []

    def add(name, tcode, value, unit="count", category="application", detail=""):
        if value is None:
            return
        warn, crit = _THRESHOLDS.get(name, (None, None))
        metrics.append(MetricResult(
            name=name, value=float(value), display_value=f"{int(value)} count",
            status=_grade(name, value), threshold_warning=warn, threshold_critical=crit,
            source="rfc_collector", tcode=tcode, detail=detail,
            category=category, unit=unit,
            extra_data={"collector": "RFC_TABLE"},
        ))

    _st22_from_snap(session, today, metrics)

    cancelled = session.read_table("TBTCO", ["JOBNAME"], f"STATUS = 'A' AND SDLSTRTDT = '{today}'")
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
    trfc = session.read_table("ARFCSSTATE", ["ARFCDATUM", "ARFCDEST"],
                              "ARFCSTATE = 'SYSFAIL'", limit)
    if trfc is not None:
        metrics.extend(_sm58_sysfail_metrics(trfc, today, limit))
    else:
        # Field list rejected on this release: fall back to the old count,
        # all dates, and say what it is.
        trfc = session.read_table("ARFCSSTATE", ["ARFCIPID"], "ARFCSTATE = 'SYSFAIL'", 500)
        trfc_capped = trfc is not None and len(trfc) >= 500
        add("sap.sm58.stuck_entries", "SM58", None if trfc is None else len(trfc),
            detail=("SYSFAIL tRFC entries, all dates"
                    + ("; read limit of 500 reached, so at least 500" if trfc_capped else "")),
            category="interface")

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
            _park(system, session.error or "")
            return [], session.error

        clear_cooldown(system)
        metrics = _from_function_module(session)
        if not metrics:
            metrics = _from_standard_modules(session)
        if not metrics:
            return [], "connected, but no metrics could be read"
        return metrics, None

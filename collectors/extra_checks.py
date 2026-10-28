"""
Extra SAP checks: the problems that cost a night call because nothing
looked at them.

    sap.strust.cert_days_left       SSL certificate closest to expiry (ICM HTTPS
                                    port, web dispatcher, any host:port listed)
    sap.slicense.days_left          SAP licence expiry (where readable)
    sap.sm37.missed_jobs            expected jobs that did not finish by their time
    sap.sm36.housekeeping_missing   SAP standard housekeeping jobs not run recently
    sap.sm37.overdue_jobs           running jobs far past their usual runtime
    sap.su01.user_issues            watched users locked / expired / expiring
    sap.sm59.failing_destinations   RFC destinations with failed calls (SM58) or
                                    failing a connection test
    sap.sp01.spool_requests         spool requests (and % of the spool number range)
    sap.stms.failed_imports         transports imported with RC >= 8 (24 h)
    hana.*                          HANA licence, memory vs allocation limit,
                                    current high alerts, system replication,
                                    last data backup (needs a `hana:` block)

HOW NOTHING IS GUESSED
Every read is either a plain table read through RFC_READ_TABLE on a table
whose fields are checked first (DDIF_FIELDINFO_GET), or a function module
whose interface is read first (pyrfc get_function_description) and called
only when it has the expected parameters. A read a system does not offer
gives that check status UNKNOWN with the reason -- never a made-up value
and never an exception into the live read or the sweep.
`python scripts/probe_checks.py <SYSTEM>` prints, per check, which source
works on that system.

Configuration (all optional), per system in systems.yaml:
    checks:
      certificates: ["sapprd.example.com:44300", "10.1.0.50:443"]
      expected_jobs:
        - {job: Z_DAILY_BILLING, by: "06:00", days: mon-fri, severity: critical}
      watch_users: [ALEREMOTE, WF-BATCH]
      rfc_destinations: [BWPCLNT100, CRM_PRD]
      spool_max: 32000
    hana: {host: 10.1.0.60, port: 30015, user: MONITOR, password: ${PS4_HANA_PASSWORD}}
and defaults in config/checks.yaml.

Results are kept per system for IBO_EXTRA_CHECKS_MINUTES (default 30) in
SQLite, shared by every process: these values move over hours and days,
and several reads scan tables. They are run by core/check_alerts.py in the
RFC worker process (core/rfc_proc.py); the live wall only reads them.
"""
from __future__ import annotations

import os
import re
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__)

_cache: dict[str, tuple[float, list]] = {}
_cache_lock = threading.Lock()
_iface_cache: dict[tuple, object] = {}
_fields_cache: dict[tuple, object] = {}
_NAME_OK = re.compile(r"^[A-Za-z0-9_/\-.#$]{1,60}$")

TCODES = {
    "sap.strust.cert_days_left": ("STRUST", "Certificate expiry"),
    "sap.slicense.days_left": ("SLICENSE", "Licence expiry"),
    "sap.sm37.missed_jobs": ("SM37", "Expected jobs missed"),
    "sap.sm36.housekeeping_missing": ("SM36", "Housekeeping jobs"),
    "sap.sm37.overdue_jobs": ("SM37", "Jobs running long"),
    "sap.su01.user_issues": ("SU01", "Watched users"),
    "sap.sm59.failing_destinations": ("SM59", "RFC destinations"),
    "sap.sp01.spool_requests": ("SP01", "Spool requests"),
    "sap.sp01.spool_fill_pct": ("SP01", "Spool number range"),
    "sap.stms.failed_imports": ("STMS", "Failed imports (24 h)"),
    "hana.license_days_left": ("HANA", "HANA licence"),
    "hana.memory_used_pct": ("HANA", "HANA memory / limit"),
    "hana.alerts_high": ("HANA", "HANA high alerts"),
    "hana.replication": ("HANA", "System replication"),
    "hana.last_backup_hours": ("HANA", "Last data backup"),
}

DEFAULT_HOUSEKEEPING = [
    {"job": "SAP_REORG_JOBS", "within_days": 8},
    {"job": "SAP_REORG_SPOOL", "within_days": 8},
    {"job": "SAP_REORG_BATCHINPUT", "within_days": 8},
    {"job": "SAP_REORG_ABAPDUMPS", "within_days": 8},
    {"job": "SAP_REORG_UPDATERECORDS", "within_days": 8},
    {"job": "SAP_COLLECTOR_FOR_PERFMONITOR", "within_days": 1},
]


# ------------------------------------------------------------------ config

_settings_cache: dict = {"stamp": None, "data": {}}


def settings() -> dict:
    try:
        from core.config_loader import CONFIG_DIR
        path = os.path.join(CONFIG_DIR, "checks.yaml")
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
        if _settings_cache["stamp"] != stamp:
            import yaml
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            _settings_cache.update(stamp=stamp, data=data if isinstance(data, dict) else {})
        return _settings_cache["data"]
    except OSError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"config/checks.yaml unreadable: {exc}")
        return {}


def _days(key: str, default: float) -> float:
    try:
        return float((settings().get("days") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.getenv("IBO_EXTRA_CHECKS", "1").strip().lower() not in ("0", "false", "no", "off")


def ttl_seconds() -> float:
    try:
        return float(os.getenv("IBO_EXTRA_CHECKS_MINUTES", "30") or 30) * 60
    except ValueError:
        return 1800.0


# ---------------------------------------------------------------- helpers

def _metric(name: str, value, status: Status, detail: str, display: str | None = None,
            warn=None, crit=None, extra: dict | None = None) -> MetricResult:
    tcode = TCODES.get(name, ("—", name))[0]
    m = MetricResult(name=name, value=value if isinstance(value, (int, float)) and not isinstance(value, bool)
                     else None, display_value=display if display is not None else
                     ("—" if value is None else str(value)), status=status, tcode=tcode,
                     detail=str(detail or "")[:400], source="RFC · checks",
                     category="checks", threshold_warning=warn, threshold_critical=crit)
    m.extra_data = dict(extra or {}, graded=True)
    return m


def _unknown(name: str, why: str) -> MetricResult:
    return _metric(name, None, Status.UNKNOWN, why, display="n/a")


def _lower_is_worse(value: float, warn: float, crit: float) -> Status:
    if value <= crit:
        return Status.CRITICAL
    if value <= warn:
        return Status.WARNING
    return Status.NORMAL


def _sap_now(system: str) -> datetime:
    """The SAP system's local time NOW: the clock learned at the last
    performance read (collectors/rfc_perf.py) moved on by the time that has
    passed since, so a check run later (check alerts thread, a gap in
    polling) does not judge jobs against an old time."""
    try:
        from collectors import sap_clock
        if sap_clock.is_learned(system):
            return sap_clock.sap_now(system)      # host clock + learned offset
    except Exception:
        pass
    # Learned in another process (live reads run in their own workers): the
    # stored offset SAP time - host time, if learned within the last day.
    try:
        from core import ops_db
        row = ops_db.one("SELECT offset_s, at FROM sap_clock WHERE system=?", (system,))
        when = ops_db.parse(row["at"]) if row else None
        if row and when and datetime.now() - when < timedelta(days=1):
            return datetime.now() + timedelta(seconds=float(row["offset_s"]))
    except Exception:
        pass
    return datetime.now()


def _ident(name: str) -> str | None:
    """A job / user / destination name safe to put in a WHERE clause, upper-cased."""
    name = str(name or "").strip().upper()
    return name if _NAME_OK.match(name) else None


_IFACE_TTL = 12 * 3600      # support packages can add or change function modules


def _cached_lookup(cache: dict, key: tuple):
    hit = cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _IFACE_TTL:
        return True, hit[1]
    return False, None


def iface(session, fm: str) -> dict | None:
    """{param name: description dict} of a function module, or None when it does not exist.

    "Does not exist" is remembered (12 h); a lookup that failed for another
    reason (connection dropped, timeout) is not, so it is tried again next run.
    """
    key = (session.system, fm)
    found, value = _cached_lookup(_iface_cache, key)
    if found:
        return value
    try:
        desc = session.conn.get_function_description(fm)
        out = {p["name"]: p for p in desc.parameters}
    except Exception as exc:  # noqa: BLE001
        text = str(exc).upper()
        if not any(k in text for k in ("FU_NOT_FOUND", "NOT_FOUND", "NOT FOUND", "DOES NOT EXIST")):
            return None
        out = None
    _iface_cache[key] = (time.monotonic(), out)
    return out


def table_fields(session, table: str) -> set[str] | None:
    """Field names of a table (DDIF_FIELDINFO_GET), or None when unknown (not remembered)."""
    key = (session.system, table)
    found, value = _cached_lookup(_fields_cache, key)
    if found:
        return value
    got = session.call("DDIF_FIELDINFO_GET", TABNAME=table)
    if not got:
        return None
    rows = got.get("DFIES_TAB") or []
    out = {str(r.get("FIELDNAME", "")).strip() for r in rows if r.get("FIELDNAME")} or None
    if out:
        _fields_cache[key] = (time.monotonic(), out)
    return out


def _rows(session, table: str, fields: list[str], where: str, rows: int = 500) -> list[dict] | None:
    got = session.read_table(table, fields, where, rows=rows)
    if got is None:
        return None
    return [{f: (r[i].strip() if i < len(r) else "") for i, f in enumerate(fields)} for r in got]


def _stamp(d: str, t: str = "000000") -> datetime | None:
    d, t = (d or "").strip(), (t or "").strip() or "000000"
    if len(d) != 8 or not d.isdigit() or d == "00000000":
        return None
    try:
        return datetime.strptime(d + t.zfill(6)[:6], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def forget(system: str | None = None) -> None:
    with _cache_lock:
        if system is None:
            _cache.clear()
        else:
            _cache.pop(system, None)
    for c in (_iface_cache, _fields_cache):
        for k in [k for k in c if system is None or k[0] == system]:
            c.pop(k, None)
    try:
        from core import ops_db
        if system is None:
            ops_db.execute("DELETE FROM extra_check_results")
        else:
            ops_db.execute("DELETE FROM extra_check_results WHERE system=?", (system,))
    except Exception:
        pass


# ---------------------------------------------------------- certificates

def cert_expiry(host: str, port: int, timeout: float = 4.0) -> tuple[datetime, str]:
    """(notAfter UTC, subject CN) of the certificate a TLS endpoint presents. Raises on failure."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    is_ip = bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host))
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=None if is_ip else host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("no certificate presented")
    pem = ssl.DER_cert_to_PEM_cert(der)
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        nva = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=timezone.utc)
        cn = ""
        try:
            cn = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
        except Exception:
            pass
        return nva.astimezone(timezone.utc).replace(tzinfo=None), str(cn)
    except ImportError:
        pass
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(pem)
        decoded = ssl._ssl._test_decode_cert(path)  # noqa: SLF001 -- stable since 2.7, used by CPython's tests
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    nva = datetime.fromtimestamp(ssl.cert_time_to_seconds(decoded["notAfter"]), tz=timezone.utc)
    cn = ""
    for part in decoded.get("subject", ()):
        for k, v in part:
            if k == "commonName":
                cn = v
    return nva.replace(tzinfo=None), cn


def _cert_targets(cfg: dict) -> list[tuple[str, int, bool]]:
    """(host, port, explicit). Explicit targets are always tried; the derived one only if it answers."""
    out = []
    for t in ((cfg.get("checks") or {}).get("certificates") or []):
        host, _, port = str(t).rpartition(":")
        if host and port.isdigit():
            out.append((host, int(port), True))
    rfc = cfg.get("rfc") or {}
    host = str(rfc.get("ashost") or "").strip()
    sysnr = str(rfc.get("sysnr") or "00").zfill(2)
    if host and not host.startswith("127.") and not out:
        out.append((host, int(f"443{sysnr}"), False))
    return out


def check_certificates(cfg: dict) -> MetricResult | None:
    targets = _cert_targets(cfg)
    if not targets:
        return None
    warn, crit = _days("cert_warning", 30), _days("cert_critical", 7)
    found, problems = [], []
    for host, port, explicit in targets:
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError:
            if explicit:
                problems.append(f"{host}:{port} does not answer")
            continue
        try:
            nva, cn = cert_expiry(host, port)
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            found.append(((nva - now_utc).total_seconds() / 86400, f"{cn or host}:{port}", nva))
        except Exception as exc:  # noqa: BLE001
            if explicit:
                problems.append(f"{host}:{port}: {type(exc).__name__}: {exc}"[:120])
    if not found:
        if problems:
            return _unknown("sap.strust.cert_days_left", "; ".join(problems))
        return None
    found.sort()
    days, where, nva = found[0]
    days_i = int(days)
    detail = f"{where} expires {nva:%Y-%m-%d}" + (f" ({len(found)} checked)" if len(found) > 1 else "")
    if problems:
        detail += "; " + "; ".join(problems)
    return _metric("sap.strust.cert_days_left", days_i, _lower_is_worse(days, warn, crit), detail,
                   display=f"{days_i} days", warn=warn, crit=crit,
                   extra={"certificates": [{"where": w, "expires": n.strftime("%Y-%m-%d"), "days": int(d)}
                                           for d, w, n in found]})


# --------------------------------------------------------------- licence

def check_licence(session) -> MetricResult:
    """
    The SAP licence expiry. There is no documented remote-enabled reader for
    SLICENSE on every release, so the candidates in config/checks.yaml
    (licence_function_modules) are each inspected first: one that exists and
    returns a table with an expiry-like date field is used.
    """
    cands = settings().get("licence_function_modules") or ["SLIC_GET_LICENCE_KEYS", "SLIC_LIKEY_GET_ALL_LICENSES"]
    tried = []
    for fm in cands:
        params = iface(session, fm)
        if params is None:
            tried.append(f"{fm} not on this system")
            continue
        required = [n for n, p in params.items() if p.get("direction") == "RFC_IMPORT" and not p.get("optional")]
        if required:
            tried.append(f"{fm} needs {','.join(required)}")
            continue
        got = session.call(fm)
        if got is None:
            tried.append(f"{fm}: {session.last_error[:80]}")
            continue
        dates = []
        for value in got.values():
            if not isinstance(value, list):
                continue
            for row in value:
                if not isinstance(row, dict):
                    continue
                for k, v in row.items():
                    if re.search(r"EXPIR|VALID_?TO|END_?DATE", str(k), re.I):
                        d = str(v).strip()
                        if len(d) == 8 and d.isdigit() and d != "00000000":
                            dates.append(d)
        if not dates:
            tried.append(f"{fm}: no expiry field in its answer")
            continue
        if all(d >= "99990000" for d in dates):
            return _metric("sap.slicense.days_left", None, Status.NORMAL, f"permanent licence ({fm})",
                           display="permanent")
        nearest = min(d for d in dates if d < "99990000")
        exp = datetime.strptime(nearest, "%Y%m%d")
        days = (exp.date() - datetime.now().date()).days
        warn, crit = _days("licence_warning", 30), _days("licence_critical", 7)
        return _metric("sap.slicense.days_left", days, _lower_is_worse(days, warn, crit),
                       f"licence expires {exp:%Y-%m-%d} ({fm})", display=f"{days} days", warn=warn, crit=crit)
    return _unknown("sap.slicense.days_left",
                    "licence expiry not readable over RFC here (" + "; ".join(tried)[:200] +
                    "). Check SLICENSE by hand, or add a hana: block for the HANA licence.")


# ------------------------------------------------------------------- jobs

_DAYNAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _clock(value) -> str | None:
    """'06:00' -> '06:00'. Also accepts what YAML makes of an unquoted 06:00
    (the integer 360: YAML 1.1 reads h:mm as base-60). None when invalid."""
    if value is None or value == "":
        return "23:59"
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        hh, mm = divmod(value, 60)
    else:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
        if not m:
            return None
        hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"


def _due_today(spec: str, when: datetime) -> bool:
    spec = str(spec or "daily").strip().lower()
    if spec in ("daily", "*", ""):
        return True
    if spec.startswith("monthly:"):
        try:
            return when.day in {int(x) for x in spec.split(":", 1)[1].split(",") if x.strip()}
        except ValueError:
            return False
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a in _DAYNAMES and b in _DAYNAMES:
                i, j = _DAYNAMES.index(a), _DAYNAMES.index(b)
                days |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
        elif part in _DAYNAMES:
            days.add(_DAYNAMES.index(part))
    return when.weekday() in days


def check_expected_jobs(session, system: str, cfg: dict) -> MetricResult | None:
    jobs = list(((cfg.get("checks") or {}).get("expected_jobs") or []))
    jobs += [j for j in (settings().get("expected_jobs") or {}).get(system, []) or []]
    if not jobs:
        return None
    now = _sap_now(system)
    today = now.strftime("%Y%m%d")
    missed, late_ok, bad_cfg, worst = [], 0, [], Status.NORMAL
    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = _ident(j.get("job"))
        by = _clock(j.get("by"))
        if not name or by is None:
            bad_cfg.append(str(j.get("job")))
            continue
        if not _due_today(j.get("days"), now):
            continue
        hh, mm = (int(x) for x in by.split(":"))
        if now < now.replace(hour=hh, minute=mm, second=0, microsecond=0):
            continue
        rows = _rows(session, "TBTCO", ["JOBNAME", "STATUS", "ENDDATE", "ENDTIME"],
                     f"JOBNAME = '{name}' AND SDLSTRTDT = '{today}'", rows=50)
        if rows is None:
            return _unknown("sap.sm37.missed_jobs", f"TBTCO not readable: {session.last_error[:120]}")
        done = [r for r in rows if r["STATUS"] == "F"]
        if done:
            late_ok += 1
            continue
        state = ("running" if any(r["STATUS"] == "R" for r in rows) else
                 "cancelled" if any(r["STATUS"] == "A" for r in rows) else
                 "released, not started" if any(r["STATUS"] in ("S", "Y", "Z") for r in rows) else
                 "not scheduled today")
        missed.append(f"{name} ({state}, due {by})")
        sev = str(j.get("severity") or "warning").lower()
        if sev == "critical":
            worst = Status.CRITICAL
        elif worst != Status.CRITICAL:
            worst = Status.WARNING
    detail = ("; ".join(missed) if missed else f"all {late_ok} due job(s) finished") + (
        f"; invalid entries: {', '.join(bad_cfg)}" if bad_cfg else "")
    return _metric("sap.sm37.missed_jobs", len(missed), worst if missed else Status.NORMAL, detail,
                   extra={"missed": missed})


def check_housekeeping(session, system: str) -> MetricResult | None:
    jobs = settings().get("housekeeping_jobs")
    if jobs is None:
        jobs = DEFAULT_HOUSEKEEPING
    if not jobs:
        return None
    now = _sap_now(system)
    missing = []
    for j in jobs:
        name = _ident((j or {}).get("job"))
        if not name:
            continue
        since = (now - timedelta(days=float(j.get("within_days") or 8))).strftime("%Y%m%d")
        rows = _rows(session, "TBTCO", ["JOBNAME"], f"JOBNAME = '{name}' AND STATUS = 'F' AND ENDDATE >= '{since}'",
                     rows=1)
        if rows is None:
            return _unknown("sap.sm36.housekeeping_missing", f"TBTCO not readable: {session.last_error[:120]}")
        if not rows:
            missing.append(f"{name} (none finished in {j.get('within_days') or 8} d)")
    return _metric("sap.sm36.housekeeping_missing", len(missing),
                   Status.WARNING if missing else Status.NORMAL,
                   "; ".join(missing) if missing else "standard housekeeping jobs ran",
                   extra={"missing": missing})


_hist_cache: dict[tuple, tuple[float, list[float]]] = {}


def _runtime_history(session, system: str, job: str, now: datetime) -> list[float] | None:
    key = (system, job)
    hit = _hist_cache.get(key)
    if hit and time.monotonic() - hit[0] < 6 * 3600:
        return hit[1]
    since = (now - timedelta(days=30)).strftime("%Y%m%d")
    rows = _rows(session, "TBTCO", ["STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME"],
                 f"JOBNAME = '{job}' AND STATUS = 'F' AND ENDDATE >= '{since}'", rows=200)
    if rows is None:
        return None
    out = []
    for r in rows:
        a, b = _stamp(r["STRTDATE"], r["STRTTIME"]), _stamp(r["ENDDATE"], r["ENDTIME"])
        if a and b and b >= a:
            out.append((b - a).total_seconds())
    _hist_cache[key] = (time.monotonic(), out)
    return out


def _p90(values: list[float]) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * 0.9
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def check_job_runtimes(session, system: str, cfg: dict | None = None) -> MetricResult | None:
    """Running jobs far past their usual runtime (p90 of the last 30 days).

    Jobs whose runtime varies by design (SLT load jobs, interfaces that wait
    for data) can be left out: checks.runtime_ignore: ["/1LT/*", "Z_IDOC_*"]
    per system in systems.yaml, or runtime_ignore in config/checks.yaml."""
    import fnmatch
    ignore = [str(p).upper() for p in (((cfg or {}).get("checks") or {}).get("runtime_ignore") or [])]
    ignore += [str(p).upper() for p in (settings().get("runtime_ignore") or [])]
    now = _sap_now(system)
    running = _rows(session, "TBTCO", ["JOBNAME", "JOBCOUNT", "STRTDATE", "STRTTIME"], "STATUS = 'R'", rows=100)
    if running is None:
        return _unknown("sap.sm37.overdue_jobs", f"TBTCO not readable: {session.last_error[:120]}")
    overdue, judged = [], 0
    for r in running[:15]:
        name = _ident(r["JOBNAME"])
        start = _stamp(r["STRTDATE"], r["STRTTIME"])
        if not name or not start or any(fnmatch.fnmatchcase(name.upper(), p) for p in ignore):
            continue
        elapsed = (now - start).total_seconds()
        if elapsed < 600:
            continue
        hist = _runtime_history(session, system, name, now)
        if not hist or len(hist) < 3:
            continue
        judged += 1
        p90 = _p90(hist)
        limit = max(p90 * 1.5, p90 + 900)
        if elapsed > limit:
            overdue.append(f"{name} running {int(elapsed // 60)} min, normally finishes within "
                           f"{int(p90 // 60) or 1} min (9 out of 10 of its last {len(hist)} runs)")
    return _metric("sap.sm37.overdue_jobs", len(overdue), Status.WARNING if overdue else Status.NORMAL,
                   "; ".join(overdue) if overdue else
                   f"{len(running)} running, {judged} with enough history -- none overdue",
                   extra={"overdue": overdue})


# ------------------------------------------------------------------ users

def check_users(session, system: str, cfg: dict) -> MetricResult | None:
    names = [_ident(u) for u in ((cfg.get("checks") or {}).get("watch_users") or [])]
    rfc_user = _ident((cfg.get("rfc") or {}).get("username"))
    if rfc_user:
        names.append(rfc_user)
    names = [n for n in dict.fromkeys(names) if n]
    if not names:
        return None
    warn_days = _days("user_validity_warning", 14)
    today = datetime.now().strftime("%Y%m%d")
    issues, worst = [], Status.NORMAL
    for n in names[:20]:
        rows = _rows(session, "USR02", ["BNAME", "USTYP", "UFLAG", "GLTGV", "GLTGB"], f"BNAME = '{n}'", rows=1)
        if rows is None:
            return _unknown("sap.su01.user_issues", f"USR02 not readable (needs S_TABU_DIS for SUSR): "
                                                    f"{session.last_error[:100]}")
        if not rows:
            issues.append(f"{n} does not exist")
            worst = Status.CRITICAL
            continue
        r = rows[0]
        try:
            flag = int(r["UFLAG"] or 0)
        except ValueError:
            flag = 0
        if flag:
            why = "locked after failed logons" if flag & 128 else "locked by an administrator" if flag & 64 else \
                "locked" + (f" (UFLAG {flag})")
            issues.append(f"{n} {why}")
            worst = Status.CRITICAL
        gltgb = r["GLTGB"]
        if gltgb and gltgb != "00000000":
            if gltgb < today:
                issues.append(f"{n} validity ended {gltgb[:4]}-{gltgb[4:6]}-{gltgb[6:]}")
                worst = Status.CRITICAL
            else:
                left = (datetime.strptime(gltgb, "%Y%m%d").date() - datetime.now().date()).days
                if left <= warn_days:
                    issues.append(f"{n} valid only until {gltgb[:4]}-{gltgb[4:6]}-{gltgb[6:]} ({left} d)")
                    if worst == Status.NORMAL:
                        worst = Status.WARNING
    return _metric("sap.su01.user_issues", len(issues), worst,
                   "; ".join(issues) if issues else f"{len(names)} watched user(s) OK: {', '.join(names)}",
                   extra={"issues": issues, "users": names})


# --------------------------------------------------------- RFC destinations

def check_destinations(session, system: str, cfg: dict) -> MetricResult | None:
    failing: dict[str, int] = {}
    notes = []
    # Only RECENT failures (checks.sm58_days, default 2): an old SM58 backlog
    # is not a destination failing now -- the SM58 backlog itself is a
    # separate wall check. The date field is used only if the table has it.
    try:
        days = max(1, int((cfg.get("checks") or {}).get("sm58_days") or settings().get("sm58_days") or 2))
    except (TypeError, ValueError):
        days = 2
    where = "( ARFCSTATE = 'SYSFAIL' OR ARFCSTATE = 'CPICERR' )"
    fields = table_fields(session, "ARFCSSTATE") or set()
    window = ""
    if "ARFCDATUM" in fields:
        since = (_sap_now(system) - timedelta(days=days - 1)).strftime("%Y%m%d")
        where += f" AND ARFCDATUM >= '{since}'"
        window = f" in {days} d"
    rows = _rows(session, "ARFCSSTATE", ["ARFCDEST", "ARFCSTATE"], where, rows=5000)
    if rows is None:
        notes.append(f"ARFCSSTATE not readable ({session.last_error[:80]})")
    else:
        for r in rows:
            failing[r["ARFCDEST"] or "?"] = failing.get(r["ARFCDEST"] or "?", 0) + 1
    tested = []
    dests = [_ident(d) for d in ((cfg.get("checks") or {}).get("rfc_destinations") or [])]
    dests = [d for d in dests if d]
    if dests:
        params = iface(session, "CAT_CHECK_RFC_DESTINATION")
        if params is None or "RFCDESTINATION" not in params:
            notes.append("connection test function CAT_CHECK_RFC_DESTINATION not available")
        else:
            for d in dests[:15]:
                got = session.call("CAT_CHECK_RFC_DESTINATION", RFCDESTINATION=d)
                if got is None:
                    notes.append(f"{d}: test not callable ({session.last_error[:80]})")
                    break
                rc = str(got.get("RFC_SUBRC", "0")).strip()
                if rc not in ("", "0"):
                    msg = " ".join(str(got.get(k, "")).strip() for k in ("MSGV1", "MSGV2")).strip()
                    failing[d] = failing.get(d, 0) + 1
                    tested.append(f"{d} test failed (rc {rc}{': ' + msg if msg else ''})")
    if rows is None and not dests:
        return _unknown("sap.sm59.failing_destinations", "; ".join(notes))
    parts = [f"{d}: {n} failed call(s){window}" for d, n in sorted(failing.items(), key=lambda kv: -kv[1])
             if not any(t.startswith(d + " test") for t in tested)][:6] + tested
    return _metric("sap.sm59.failing_destinations", len(failing), Status.WARNING if failing else Status.NORMAL,
                   "; ".join(parts + notes) if (parts or notes) else f"no failed tRFC calls{window}; tests OK",
                   extra={"failing": failing})


# ------------------------------------------------------------------- spool

def _count_rows(session, table: str) -> int | None:
    params = iface(session, "EM_GET_NUMBER_OF_ENTRIES")
    if params is None or "IT_TABLES" not in params:
        return None
    got = session.call("EM_GET_NUMBER_OF_ENTRIES", IT_TABLES=[{"TABNAME": table}])
    if not got:
        return None
    for r in got.get("IT_TABLES") or []:
        if str(r.get("TABNAME", "")).strip() == table:
            try:
                return int(r.get("TABROWS"))
            except (TypeError, ValueError):
                return None
    return None


def _param_value(session, name: str) -> str | None:
    params = iface(session, "TH_GET_PARAMETER")
    if params is None:
        return None
    in_name = next((n for n in ("NAME", "PARAMETER_NAME", "PARAMETER") if n in params), None)
    if not in_name:
        return None
    got = session.call("TH_GET_PARAMETER", **{in_name: name})
    if not got:
        return None
    for k, v in got.items():
        if "VALUE" in k.upper() and str(v).strip():
            return str(v).strip()
    return None


def check_spool(session, cfg: dict) -> MetricResult | None:
    count = _count_rows(session, "TSP01")
    if count is None:
        return _unknown("sap.sp01.spool_requests", "row count function EM_GET_NUMBER_OF_ENTRIES not available "
                                                    "-- spool size not measured")
    mx = (cfg.get("checks") or {}).get("spool_max")
    source = "systems.yaml"
    if not mx:
        raw = _param_value(session, "rspo/spool_id/max_number")
        mx, source = (raw, "rspo/spool_id/max_number") if raw else (None, "")
    try:
        mx = int(mx) if mx else None
    except (TypeError, ValueError):
        mx = None
    if mx:
        pct = round(100.0 * count / mx, 1)
        st = Status.CRITICAL if pct >= 90 else Status.WARNING if pct >= 80 else Status.NORMAL
        return _metric("sap.sp01.spool_requests", count, st,
                       f"{count} of {mx} spool numbers in use ({pct}%, limit from {source}); "
                       f"run RSPO1041 / SAP_REORG_SPOOL", extra={"fill_pct": pct, "max": mx})
    return _metric("sap.sp01.spool_requests", count, Status.NORMAL,
                   f"{count} spool requests (number range size unknown -- set checks.spool_max to grade it)")


# -------------------------------------------------------------- transports

def recent_imports(session, system: str, days: float = 7) -> tuple[list[dict] | None, str]:
    """Imports in the last `days` from TPALOG: [{trkorr, rc, at}], or (None, why)."""
    fields = table_fields(session, "TPALOG")
    if not fields:
        return None, "TPALOG not readable on this release"
    need = {"TRKORR", "RETCODE", "TRTIME"}
    if not need <= fields:
        return None, f"TPALOG lacks {sorted(need - fields)}"
    since = (_sap_now(system) - timedelta(days=days)).strftime("%Y%m%d%H%M%S")
    cols = ["TRKORR", "RETCODE", "TRTIME"] + [f for f in ("TRSTEP", "TRCLI") if f in fields]
    rows = _rows(session, "TPALOG", cols, f"TRTIME >= '{since}'", rows=2000)
    if rows is None:
        return None, f"TPALOG read failed: {session.last_error[:100]}"
    # TPALOG has one row per import STEP (and client). One entry per import
    # RUN of a transport: steps within 2 hours of each other belong to one
    # run, which gets its worst return code. A failed import that was
    # imported again cleanly is then judged by the clean run.
    per: dict[str, list[dict]] = {}
    for r in rows:
        try:
            rc = int(str(r["RETCODE"]).strip() or 0)
        except ValueError:
            rc = 0
        at = str(r["TRTIME"]).strip()[:14]
        per.setdefault(str(r["TRKORR"]).strip(), []).append({"rc": rc, "at": at, "step": r.get("TRSTEP", "")})
    out = []
    for tr, steps in per.items():
        steps.sort(key=lambda x: x["at"])
        run: dict | None = None
        for st in steps:
            try:
                gap = (datetime.strptime(st["at"], "%Y%m%d%H%M%S")
                       - datetime.strptime(run["at"], "%Y%m%d%H%M%S")).total_seconds() if run else None
            except ValueError:
                gap = 0
            if run is None or (gap is not None and gap > 7200):
                if run is not None:
                    out.append(run)
                run = {"trkorr": tr, "rc": st["rc"], "at": st["at"], "step": st["step"], "earlier": False}
            else:
                if st["rc"] > run["rc"]:
                    run["rc"], run["step"] = st["rc"], st["step"]
                run["at"] = st["at"]
        if run is not None:
            out.append(run)
    # Mark runs superseded by a later run of the same transport.
    latest = {}
    for e in out:
        if e["trkorr"] not in latest or e["at"] > latest[e["trkorr"]]["at"]:
            latest[e["trkorr"]] = e
    for e in out:
        e["earlier"] = e is not latest[e["trkorr"]]
    out.sort(key=lambda e: e["at"])
    return [{k: v for k, v in e.items() if k != "earlier"} | ({"superseded": True} if e["earlier"] else {})
            for e in out], ""


def check_transports(session, system: str) -> MetricResult:
    imports, why = recent_imports(session, system, days=1)
    if imports is None:
        return _unknown("sap.stms.failed_imports", why)
    failed: dict[str, int] = {}
    for i in imports:
        if i["rc"] >= 8 and not i.get("superseded"):      # a later clean run settles it
            failed[i["trkorr"]] = max(failed.get(i["trkorr"], 0), i["rc"])
    done = sorted({i["trkorr"] for i in imports})
    st = (Status.CRITICAL if any(rc >= 12 for rc in failed.values()) else
          Status.WARNING if failed else Status.NORMAL)
    return _metric("sap.stms.failed_imports", len(failed), st,
                   ("; ".join(f"{k} RC {v}" for k, v in sorted(failed.items())) if failed else
                    f"{len(done)} transport(s) imported in 24 h, none failed"),
                   extra={"failed": failed, "imported": done[:50]})


# -------------------------------------------------------------------- HANA

def check_hana(system: str, cfg: dict) -> list[MetricResult]:
    h = cfg.get("hana") or {}
    if not isinstance(h, dict) or not h.get("host"):
        return []
    try:
        from hdbcli import dbapi
    except ImportError:
        return [_unknown("hana.memory_used_pct", "hdbcli is not installed: pip install hdbcli")]
    out: list[MetricResult] = []
    try:
        conn = dbapi.connect(address=str(h["host"]), port=int(h.get("port") or 30015),
                             user=str(h.get("user") or ""), password=str(h.get("password") or ""),
                             connectTimeout=8000, communicationTimeout=20000)
    except Exception as exc:  # noqa: BLE001
        return [_unknown("hana.memory_used_pct", f"HANA logon failed: {type(exc).__name__}: {str(exc)[:150]}")]
    try:
        cur = conn.cursor()

        def q(sql):
            cur.execute(sql)
            return cur.fetchall()

        try:
            rows = q("SELECT EXPIRATION_DATE, PERMANENT, VALID FROM M_LICENSE")
            if rows:
                exp, perm, valid = rows[0]
                if str(valid).upper() == "FALSE":
                    out.append(_metric("hana.license_days_left", 0, Status.CRITICAL, "HANA licence NOT valid"))
                elif str(perm).upper() == "TRUE" or exp is None:
                    out.append(_metric("hana.license_days_left", None, Status.NORMAL, "permanent licence",
                                       display="permanent"))
                else:
                    days = (exp - datetime.now()).days if isinstance(exp, datetime) else None
                    if days is None:
                        out.append(_unknown("hana.license_days_left", f"unexpected date {exp!r}"))
                    else:
                        out.append(_metric("hana.license_days_left", days,
                                           _lower_is_worse(days, _days("licence_warning", 30),
                                                           _days("licence_critical", 7)),
                                           f"expires {exp:%Y-%m-%d}", display=f"{days} days"))
        except Exception as exc:  # noqa: BLE001
            out.append(_unknown("hana.license_days_left", f"M_LICENSE: {str(exc)[:120]}"))
        try:
            rows = q("SELECT HOST, INSTANCE_TOTAL_MEMORY_USED_SIZE, ALLOCATION_LIMIT FROM M_HOST_RESOURCE_UTILIZATION")
            worst = None
            for host, used, limit in rows:
                if used is not None and limit:
                    pct = round(100.0 * float(used) / float(limit), 1)
                    if worst is None or pct > worst[0]:
                        worst = (pct, host, float(used), float(limit))
            if worst:
                pct, host, used, limit = worst
                st = Status.CRITICAL if pct >= 95 else Status.WARNING if pct >= 85 else Status.NORMAL
                out.append(_metric("hana.memory_used_pct", pct, st,
                                   f"{host}: {used / 1024 ** 3:.0f} of {limit / 1024 ** 3:.0f} GB allocation limit",
                                   display=f"{pct}%", warn=85, crit=95))
        except Exception as exc:  # noqa: BLE001
            out.append(_unknown("hana.memory_used_pct", f"M_HOST_RESOURCE_UTILIZATION: {str(exc)[:120]}"))
        try:
            rows = q("SELECT ALERT_RATING, ALERT_DETAILS FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS "
                     "WHERE ALERT_RATING >= 4")
            high = [r for r in rows if int(r[0] or 0) >= 4]
            st = (Status.CRITICAL if any(int(r[0]) >= 5 for r in high) else
                  Status.WARNING if high else Status.NORMAL)
            out.append(_metric("hana.alerts_high", len(high), st,
                               "; ".join(str(r[1])[:120] for r in high[:3]) or "no high or very high alerts"))
        except Exception as exc:  # noqa: BLE001
            out.append(_unknown("hana.alerts_high", f"statistics server alerts: {str(exc)[:120]}"))
        try:
            rows = q("SELECT SECONDARY_HOST, REPLICATION_STATUS FROM M_SERVICE_REPLICATION")
            if rows:
                bad = [f"{r[0]}: {r[1]}" for r in rows if str(r[1]).upper() != "ACTIVE"]
                out.append(_metric("hana.replication", len(bad), Status.CRITICAL if bad else Status.NORMAL,
                                   "; ".join(bad[:4]) if bad else f"{len(rows)} service(s) ACTIVE",
                                   display="not in sync" if bad else "ACTIVE"))
        except Exception as exc:  # noqa: BLE001
            out.append(_unknown("hana.replication", f"M_SERVICE_REPLICATION: {str(exc)[:120]}"))
        try:
            # UTC on both sides: the HANA server's local time zone need not be
            # the dashboard's.
            rows = q("SELECT MAX(UTC_END_TIME) FROM M_BACKUP_CATALOG WHERE ENTRY_TYPE_NAME = 'complete data backup' "
                     "AND STATE_NAME = 'successful'")
            last = rows[0][0] if rows else None
            if last is None:
                out.append(_metric("hana.last_backup_hours", None, Status.CRITICAL,
                                   "no successful complete data backup in the catalog", display="none"))
            else:
                if getattr(last, "tzinfo", None) is not None:
                    last = last.astimezone(timezone.utc).replace(tzinfo=None)
                utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                hours = round((utc_now - last).total_seconds() / 3600, 1)
                st = Status.CRITICAL if hours > 50 else Status.WARNING if hours > 26 else Status.NORMAL
                out.append(_metric("hana.last_backup_hours", hours, st,
                                   f"last complete data backup {last:%Y-%m-%d %H:%M} UTC",
                                   display=f"{hours} h", warn=26, crit=50))
        except Exception as exc:  # noqa: BLE001
            out.append(_unknown("hana.last_backup_hours", f"M_BACKUP_CATALOG: {str(exc)[:120]}"))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# ------------------------------------------------------------------- runner

def _safe(fn, *args) -> list[MetricResult]:
    try:
        got = fn(*args)
    except Exception as exc:  # noqa: BLE001 -- one check must never sink the rest
        log.warning(f"extra check {fn.__name__} failed: {type(exc).__name__}: {exc}")
        try:
            from core import errors
            errors.capture(exc, "sweep", context=f"extra check {fn.__name__}")
        except Exception:
            pass
        return []
    if got is None:
        return []
    return got if isinstance(got, list) else [got]


def run(session, system: str, cfg: dict) -> list[MetricResult]:
    """All checks for one system on an open session. Never raises."""
    out: list[MetricResult] = []
    out += _safe(check_certificates, cfg)
    if getattr(session, "ok", False):
        out += _safe(check_licence, session)
        out += _safe(check_expected_jobs, session, system, cfg)
        out += _safe(check_housekeeping, session, system)
        out += _safe(check_job_runtimes, session, system, cfg)
        out += _safe(check_users, session, system, cfg)
        out += _safe(check_destinations, session, system, cfg)
        out += _safe(check_spool, session, cfg)
        out += _safe(check_transports, session, system)
    out += _safe(check_hana, system, cfg)
    return out


# ------------------------------------------------- shared result store
# Results live in SQLite (table extra_check_results), so every process --
# the web server, its RFC worker processes, the collector and the jobs
# service -- sees the same last result and only one of them runs the
# checks per TTL (a claim on the row). The live wall only READS them: the
# checks never run inside a live read, so a slow RFC destination test or
# HANA timeout cannot hold up the wall.

def _to_json(m: MetricResult) -> dict:
    return {"name": m.name, "value": m.value, "display_value": m.display_value, "status": m.status.value,
            "threshold_warning": m.threshold_warning, "threshold_critical": m.threshold_critical,
            "source": m.source, "tcode": m.tcode, "detail": m.detail, "category": m.category,
            "extra_data": m.extra_data}


def _from_json(d: dict) -> MetricResult:
    m = MetricResult(name=d["name"], value=d.get("value"), display_value=d.get("display_value", ""),
                     status=Status(d.get("status", "UNKNOWN")), threshold_warning=d.get("threshold_warning"),
                     threshold_critical=d.get("threshold_critical"), source=d.get("source", ""),
                     tcode=d.get("tcode"), detail=d.get("detail", ""), category=d.get("category", "checks"))
    m.extra_data = dict(d.get("extra_data") or {})
    return m


def save(system: str, metrics: list[MetricResult]) -> None:
    import json
    from core import ops_db
    payload = json.dumps([_to_json(m) for m in metrics], default=str)
    ops_db.execute("INSERT INTO extra_check_results(system, at, payload, claimed_at) VALUES (?,?,?,NULL) "
                   "ON CONFLICT(system) DO UPDATE SET at=excluded.at, payload=excluded.payload, claimed_at=NULL",
                   (system, ops_db.now(), payload))


def stored(system: str, apply_limits: bool = True) -> tuple[list[MetricResult], float | None]:
    """(last saved result, its age in seconds) -- ([], None) when there is none. No RFC."""
    import json
    from core import ops_db
    try:
        row = ops_db.one("SELECT at, payload FROM extra_check_results WHERE system=?", (system,))
    except Exception:
        return [], None
    if not row or not row["at"]:
        return [], None
    at = ops_db.parse(row["at"])
    age = (datetime.now() - at).total_seconds() if at else None
    try:
        metrics = [_from_json(d) for d in json.loads(row["payload"] or "[]")]
    except (ValueError, KeyError):
        return [], None
    return ([_apply_limits(system, m) for m in metrics] if apply_limits else metrics), age


def _apply_limits(system: str, m: MetricResult) -> MetricResult:
    """
    A limit set on the Thresholds page (for all systems or this system)
    overrides the check's own rule (config/checks.yaml days, fixed counts).
    Without one the check keeps its own grading. Switched off -> NORMAL.
    Applied when results are READ, so a changed limit shows at once.
    """
    import copy
    m = copy.copy(m)            # never change the cached / stored original
    try:
        from core import grading
        lim = grading.limits_for(m.name, system)
        if not lim["enabled"]:
            if m.status in (Status.WARNING, Status.CRITICAL):
                m.status = Status.NORMAL
            return m
        if not lim["explicit"] or m.value is None:
            return m
        # Only one level set: the other stays the check's own (e.g. a
        # warning of 45 days keeps the certificate's 7-day critical).
        from core import thresholds as thr
        spec = thr.spec(m.name, system)
        w = lim["warning"] if "warning" in spec else m.threshold_warning
        c = lim["critical"] if "critical" in spec else m.threshold_critical
        low = lim["direction"] == "low"
        v = float(m.value)
        incl = lim["inclusive"]
        if c is not None and ((v <= c if incl else v < c) if low else (v >= c if incl else v > c)):
            m.status = Status.CRITICAL
        elif w is not None and (v <= w if low else v >= w):
            m.status = Status.WARNING
        else:
            m.status = Status.NORMAL
        m.threshold_warning, m.threshold_critical = w, c
    except Exception:  # noqa: BLE001 -- keep the check's own grade on any problem
        pass
    return m


def _claim(system: str) -> bool:
    """True when this process may run the checks now (nobody else is, or a run looks dead)."""
    from core import ops_db
    stale = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        ops_db.execute("INSERT OR IGNORE INTO extra_check_results(system, at, payload, claimed_at) "
                       "VALUES (?, NULL, '[]', NULL)", (system,))
        return ops_db.execute("UPDATE extra_check_results SET claimed_at=? WHERE system=? AND "
                              "(claimed_at IS NULL OR claimed_at < ?)", (ops_db.now(), system, stale)) == 1
    except Exception:
        return True


def _release(system: str) -> None:
    from core import ops_db
    try:
        ops_db.execute("UPDATE extra_check_results SET claimed_at=NULL WHERE system=?", (system,))
    except Exception:
        pass


def cached(session, system: str, cfg: dict, force: bool = False) -> tuple[list[MetricResult], float]:
    """The checks for one system, run at most once per TTL across all processes.

    Returns (metrics, age seconds), graded with the limits set on the
    Thresholds page where there are any. A result made without RFC (session
    not ok: only certificates / HANA) is returned but not stored."""
    metrics, age = _cached_raw(session, system, cfg, force)
    return [_apply_limits(system, m) for m in metrics], age


def _cached_raw(session, system: str, cfg: dict, force: bool = False) -> tuple[list[MetricResult], float]:
    if not enabled():
        return [], 0.0
    if not force:
        with _cache_lock:
            hit = _cache.get(system)
        if hit and time.monotonic() - hit[0] < ttl_seconds():
            return hit[1], time.monotonic() - hit[0]
        got, age = stored(system, apply_limits=False)
        if age is not None and age < ttl_seconds():
            with _cache_lock:
                _cache[system] = (time.monotonic() - age, got)
            return got, age
    if not getattr(session, "ok", False):
        return run(session, system, cfg), 0.0
    if not _claim(system):
        got, age = stored(system, apply_limits=False)           # another process is running them right now
        return got, (age or 0.0)
    try:
        metrics = run(session, system, cfg)
        save(system, metrics)
    finally:
        _release(system)
    with _cache_lock:
        _cache[system] = (time.monotonic(), metrics)
    return metrics, 0.0


def to_rows(metrics: list[MetricResult]) -> list[dict]:
    """Rows in the wall's checklist format (collectors/rfc_live._checks_from_metrics)."""
    rows = []
    for m in metrics:
        tcode, label = TCODES.get(m.name, (m.tcode or "—", m.name))
        rows.append({"metric": m.name, "tcode": tcode, "label": label, "value": m.display_value,
                     "status": m.status.value, "detail": (m.detail or "")[:160], "rank": 30, "risk": True,
                     "sub": "", "warning": m.threshold_warning, "critical": m.threshold_critical,
                     "graded": True})
    return rows

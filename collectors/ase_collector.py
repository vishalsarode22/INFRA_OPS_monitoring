"""
SAP ASE (Sybase) database collector -- the ST04 / DBACOCKPIT view over MDA tables.

WHY THIS DOES NOT GO THROUGH RFC
--------------------------------
DBACOCKPIT on ASE is itself a reader of the ASE monitoring tables (the
"mon*" MDA tables in master). Going through RFC would mean asking ABAP to
ask ASE, with a second authorisation layer in between and a narrower
interface. A read-only ASE login with mon_role reads the same tables
directly, with fresher data and no transport.

WHAT IT READS
-------------
    active statements        master..monProcessStatement + monProcessSQLText + monProcess
    long-running statements  same, filtered on elapsed seconds
    blocked sessions         master..monProcess where BlockingSPID > 0
    expensive statements     master..monCachedStatement ranked by UseCount * AvgLIO
    data cache hit ratio     master..monDataCache (delta between polls where possible)
    engine CPU busy          master..monEngine (delta between polls; absent on first poll)
    memory pools             sp_monitorconfig 'all' (Pct_act per pool)
    WP attribution           master..sysprocesses.hostprocess -> SAP work process PID

PREREQUISITES ON THE ASE SIDE
-----------------------------
Most MDA tables return nothing until Basis enables them:

    sp_configure 'enable monitoring', 1
    sp_configure 'statement statistics active', 1
    sp_configure 'per object statistics active', 1
    sp_configure 'wait event timing', 1
    sp_configure 'SQL batch capture', 1
    sp_configure 'max SQL text monitored', 4096

The monitoring login needs mon_role. Use a dedicated read-only login; do
not reuse SAPSR3 or sapsa.

ATTRIBUTION -- READ THIS BEFORE TRUSTING THE "USER" COLUMN
----------------------------------------------------------
Every SAP work process connects to ASE as the same login (SAPSR3). The DB
login therefore identifies nothing. What does identify the work process is
sysprocesses.hostprocess, which SAP populates with the WP's OS PID. That PID
joins to TH_WPINFO.WP_PID on the RFC side, and only there do you reach the
SAP user and the report. This module emits the PID so the RFC layer can do
the join; it does not pretend to know the SAP user itself.

FAILURE MODEL
-------------
Identical to the RFC collector: a failure returns (metrics_so_far, error)
and NEVER a healthy-looking zero. A missing driver is reported once and the
system is left with no db.ase.* metrics, which the threshold engine reads as
UNKNOWN, not NORMAL.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from core.models import MetricResult, Status
from utils.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds. (warning, critical). Overridden by config/thresholds.yaml
# where a matching key exists. Keys marked "low_is_bad" grade downwards.
# ---------------------------------------------------------------------------
_THRESHOLDS = {
    "db.ase.active_statements":        (40, 80),
    "db.ase.long_running_statements":  (1, 5),
    "db.ase.blocked_sessions":         (1, 5),
    "db.ase.expensive_statement_count": (5, 20),
    "db.ase.top_statement_avg_lio":    (100_000, 1_000_000),
    "db.ase.data_cache_hit_pct":       (95, 90),   # low is bad
    "db.ase.engine_cpu_pct":           (80, 90),
    "db.ase.memory_pool_max_used_pct": (85, 95),
}
_LOW_IS_BAD = {"db.ase.data_cache_hit_pct"}

# A statement is "long-running" past this many seconds of elapsed time.
LONG_RUNNING_SECONDS = 60
# A cached statement is "expensive" above this average logical I/O per exec.
EXPENSIVE_AVG_LIO = 50_000
# How many top statements to carry in extra_data for evidence and AI context.
TOP_N = 10

# Per-system previous samples for delta-based counters (cache hit, CPU).
# monDataCache and monEngine counters are cumulative since boot; a ratio over
# the whole uptime says nothing about the last minute.
_prev: dict[str, dict] = {}
_prev_lock = threading.Lock()


def _thresholds_for(key: str) -> tuple[float | None, float | None]:
    warn, crit = _THRESHOLDS.get(key, (None, None))
    try:
        from core.config_loader import get_thresholds
        cfg = get_thresholds().get(key) or {}
        warn = cfg.get("warning", warn)
        crit = cfg.get("critical", crit)
    except Exception:  # noqa: BLE001 - config is optional here
        pass
    return warn, crit


def _grade(key: str, value: float | None) -> Status:
    if value is None:
        return Status.UNKNOWN
    warn, crit = _thresholds_for(key)
    if key in _LOW_IS_BAD:
        if crit is not None and value <= crit:
            return Status.CRITICAL
        if warn is not None and value <= warn:
            return Status.WARNING
        return Status.NORMAL
    if crit is not None and value >= crit:
        return Status.CRITICAL
    if warn is not None and value >= warn:
        return Status.WARNING
    return Status.NORMAL


def _num(raw) -> float | None:
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _clip(text, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def build_ase_params(cfg: dict) -> dict | None:
    """
    Reads the "db:" block of a system config. Returns None when the system
    has no ASE block, so the orchestrator can skip cleanly.

        db:
          type: ase
          host: ${SYBASE_TST_HOST}
          port: ${SYBASE_TST_PORT}
          database: TST
          username: ${SYBASE_TST_USER}
          password: ${SYBASE_TST_PASS}
          driver: "Adaptive Server Enterprise"   # ODBC driver name; optional
    """
    db = (cfg or {}).get("db") or {}
    if str(db.get("type", "")).lower() not in ("ase", "sybase"):
        return None
    required = ("host", "port", "username", "password")
    missing = [k for k in required if not str(db.get(k, "")).strip()]
    if missing:
        log.warning(f"ASE config incomplete, missing: {', '.join(missing)}")
        return None
    return {
        "host": str(db["host"]).strip(),
        "port": int(str(db["port"]).strip()),
        "database": str(db.get("database", "master")).strip() or "master",
        "username": str(db["username"]).strip(),
        "password": str(db["password"]),
        "driver": str(db.get("driver", "Adaptive Server Enterprise")).strip(),
        "timeout": int(db.get("timeout", 15)),
    }


class AseSession:
    """One pyodbc connection, opened lazily, closed on exit. Never raises
    from __enter__: a failed connect leaves .ok False and .error set."""

    def __init__(self, system: str, params: dict):
        self.system = system
        self.params = params
        self.conn = None
        self.ok = False
        self.error: str | None = None

    def __enter__(self):
        try:
            import pyodbc  # local import: absence must not break module load
        except ImportError:
            self.error = "pyodbc not installed (pip install pyodbc; needs the ASE ODBC driver or FreeTDS)"
            return self
        p = self.params
        dsn = (
            f"DRIVER={{{p['driver']}}};SERVER={p['host']};PORT={p['port']};"
            f"DATABASE={p['database']};UID={p['username']};PWD={p['password']};"
        )
        try:
            self.conn = pyodbc.connect(dsn, timeout=p["timeout"], autocommit=True)
            self.ok = True
        except Exception as e:  # noqa: BLE001
            self.error = f"ASE connect failed: {type(e).__name__}: {e}"
        return self

    def __exit__(self, *exc):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def query(self, sql: str) -> list[tuple] | None:
        """Runs one statement; returns rows or None on failure (logged)."""
        if not self.ok:
            return None
        try:
            cur = self.conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
            return [tuple(r) for r in rows]
        except Exception as e:  # noqa: BLE001
            log.warning(f"[{self.system}] ASE query failed: {type(e).__name__}: {e} -- {sql[:80]}")
            return None


# ---------------------------------------------------------------------------
# Readers. Each returns (value, detail, extra) or None when unreadable.
# ---------------------------------------------------------------------------

SQL_ACTIVE = f"""
select ps.SPID, p.Login, p.Application, p.Command, p.BlockingSPID,
       datediff(ss, ps.StartTime, getdate()) as ElapsedSec,
       ps.CpuTime, ps.WaitTime, ps.LogicalReads, ps.PhysicalReads,
       sp.hostprocess,
       (select sp2.hostname from master..sysprocesses sp2 where sp2.spid = ps.SPID) as HostName,
       (select min(t.SQLText) from master..monProcessSQLText t
         where t.SPID = ps.SPID and t.BatchID = ps.BatchID and t.LineNumber = 1) as SQLText
  from master..monProcessStatement ps
  join master..monProcess p on p.SPID = ps.SPID and p.KPID = ps.KPID
  left join master..sysprocesses sp on sp.spid = ps.SPID
 where p.Command not in ('AWAITING COMMAND', 'IDLE')
"""

SQL_BLOCKED = """
select p.SPID, p.BlockingSPID, p.Login, p.Application, p.Command, p.SecondsWaiting
  from master..monProcess p
 where p.BlockingSPID > 0
"""

SQL_CACHED = f"""
select top {TOP_N * 3} SSQLID, UseCount, AvgLIO, AvgPIO, AvgElapsedTime, AvgCpuTime, LastUsedDate
  from master..monCachedStatement
 where UseCount > 0
 order by UseCount * AvgLIO desc
"""

SQL_CACHED_TEXT = "select show_cached_text({ssqlid})"

SQL_DATACACHE = "select CacheName, CacheSearches, PhysicalReads from master..monDataCache"

SQL_ENGINE = "select EngineNumber, CPUTime, IdleTime from master..monEngine"

SQL_MEMPOOLS = "exec sp_monitorconfig 'all'"


def read_active(session: AseSession):
    rows = session.query(SQL_ACTIVE)
    if rows is None:
        return None
    stmts = []
    for r in rows:
        spid, login, app, cmd, blocking, elapsed, cpu, wait, lio, pio, hostproc, host, text = r[:13]
        stmts.append({
            "spid": int(spid), "login": _clip(login, 32), "application": _clip(app, 32),
            "command": _clip(cmd, 32), "blocking_spid": int(blocking or 0),
            "elapsed_sec": int(_num(elapsed) or 0), "cpu_ms": int(_num(cpu) or 0),
            "wait_ms": int(_num(wait) or 0), "logical_reads": int(_num(lio) or 0),
            "physical_reads": int(_num(pio) or 0),
            "wp_pid": str(hostproc or "").strip(),     # joins to TH_WPINFO.WP_PID
            "host": _clip(host, 40), "sql": _clip(text, 200),
        })
    stmts.sort(key=lambda s: (s["elapsed_sec"], s["logical_reads"]), reverse=True)
    return stmts


def read_blocked(session: AseSession):
    rows = session.query(SQL_BLOCKED)
    if rows is None:
        return None
    return [{
        "spid": int(r[0]), "blocking_spid": int(r[1]), "login": _clip(r[2], 32),
        "application": _clip(r[3], 32), "command": _clip(r[4], 32),
        "seconds_waiting": int(_num(r[5]) or 0),
    } for r in rows]


def read_cached(session: AseSession, fetch_text: bool = True):
    rows = session.query(SQL_CACHED)
    if rows is None:
        return None
    out = []
    for r in rows[:TOP_N]:
        ssqlid, use, avg_lio, avg_pio, avg_el, avg_cpu, last = r[:7]
        entry = {
            "ssqlid": int(ssqlid), "exec_count": int(_num(use) or 0),
            "avg_lio": int(_num(avg_lio) or 0), "avg_pio": int(_num(avg_pio) or 0),
            "avg_elapsed_ms": int(_num(avg_el) or 0), "avg_cpu_ms": int(_num(avg_cpu) or 0),
            "total_lio_est": int((_num(use) or 0) * (_num(avg_lio) or 0)),
            "last_used": str(last or ""), "sql": "",
        }
        if fetch_text:
            t = session.query(SQL_CACHED_TEXT.format(ssqlid=int(ssqlid)))
            if t and t[0]:
                entry["sql"] = _clip(t[0][0], 300)
        out.append(entry)
    return out


def _delta_ratio(system: str, key: str, num: float, den: float) -> tuple[float | None, str]:
    """Ratio over the change since the previous poll. Falls back to the
    cumulative ratio (labelled) when there is no previous sample."""
    with _prev_lock:
        prev = _prev.setdefault(system, {}).get(key)
        _prev[system][key] = (num, den, time.monotonic())
    if prev:
        d_num, d_den = num - prev[0], den - prev[1]
        if d_den > 0 and d_num >= 0:
            return d_num / d_den, "since last poll"
    return (num / den if den > 0 else None), "cumulative since boot"


def read_cache_hit(session: AseSession, system: str):
    rows = session.query(SQL_DATACACHE)
    if rows is None:
        return None
    searches = sum(_num(r[1]) or 0 for r in rows)
    physical = sum(_num(r[2]) or 0 for r in rows)
    ratio, basis = _delta_ratio(system, "datacache", physical, searches)
    if ratio is None:
        return None
    hit = round((1.0 - ratio) * 100, 2)
    per_cache = ", ".join(f"{_clip(r[0], 24)}" for r in rows[:6])
    return hit, f"{basis}; caches: {per_cache}"


def read_engine_cpu(session: AseSession, system: str):
    rows = session.query(SQL_ENGINE)
    if rows is None:
        return None
    cpu = sum(_num(r[1]) or 0 for r in rows)
    idle = sum(_num(r[2]) or 0 for r in rows)
    with _prev_lock:
        prev = _prev.setdefault(system, {}).get("engine")
        _prev[system]["engine"] = (cpu, idle)
    if not prev:
        return None   # first poll: no honest number yet
    d_cpu, d_idle = cpu - prev[0], idle - prev[1]
    if d_cpu + d_idle <= 0:
        return None
    return round(d_cpu / (d_cpu + d_idle) * 100, 1), f"{len(rows)} engines, since last poll"


def read_memory_pools(session: AseSession):
    rows = session.query(SQL_MEMPOOLS)
    if rows is None:
        return None
    pools = []
    for r in rows:
        # sp_monitorconfig columns: Name, Num_free, Num_active, Pct_act, Max_Used, Reuse_cnt[, Instance_Name]
        name = _clip(r[0], 40) if len(r) > 0 else ""
        pct = _num(str(r[3]).strip().rstrip("%")) if len(r) > 3 else None
        if name and pct is not None:
            pools.append((name, pct))
    if not pools:
        return None
    pools.sort(key=lambda p: p[1], reverse=True)
    top = pools[0]
    detail = ", ".join(f"{n} {p:.0f}%" for n, p in pools[:5])
    return top[1], detail, [{"pool": n, "pct_used": p} for n, p in pools]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _metric(name, value, unit, category, detail="", display=None, extra=None) -> MetricResult:
    warn, crit = _thresholds_for(name)
    if display is None:
        display = f"{value:.1f}{unit}" if unit == "%" else f"{int(value)} {unit}"
    return MetricResult(
        name=name, value=float(value), display_value=display,
        status=_grade(name, value), threshold_warning=warn, threshold_critical=crit,
        source="ase_collector", tcode="ST04", detail=detail,
        category=category, unit=unit, extra_data={"collector": "ASE_MDA", **(extra or {})},
    )


def build_metrics(system: str, session: AseSession) -> list[MetricResult]:
    """Reads every section; emits only what was actually readable."""
    metrics: list[MetricResult] = []

    active = read_active(session)
    if active is not None:
        long_running = [s for s in active if s["elapsed_sec"] >= LONG_RUNNING_SECONDS]
        metrics.append(_metric(
            "db.ase.active_statements", len(active), "count", "database",
            detail="; ".join(f"SPID {s['spid']} {s['elapsed_sec']}s {s['sql'][:60]}" for s in active[:5]),
            extra={"statements": active[:TOP_N]},
        ))
        metrics.append(_metric(
            "db.ase.long_running_statements", len(long_running), "count", "database",
            detail="; ".join(
                f"SPID {s['spid']} wp_pid {s['wp_pid'] or '?'} {s['elapsed_sec']}s LIO {s['logical_reads']}"
                for s in long_running[:5]),
            extra={"statements": long_running[:TOP_N], "threshold_seconds": LONG_RUNNING_SECONDS},
        ))

    blocked = read_blocked(session)
    if blocked is not None:
        metrics.append(_metric(
            "db.ase.blocked_sessions", len(blocked), "count", "database",
            detail="; ".join(f"SPID {b['spid']} waits {b['seconds_waiting']}s on SPID {b['blocking_spid']}"
                             for b in blocked[:5]),
            extra={"blocked": blocked[:TOP_N]},
        ))

    cached = read_cached(session)
    if cached is not None:
        expensive = [c for c in cached if c["avg_lio"] >= EXPENSIVE_AVG_LIO]
        top_lio = max((c["avg_lio"] for c in cached), default=0)
        metrics.append(_metric(
            "db.ase.expensive_statement_count", len(expensive), "count", "database",
            detail="; ".join(f"#{c['ssqlid']} x{c['exec_count']} avgLIO {c['avg_lio']}" for c in expensive[:5]),
            extra={"statements": cached, "threshold_avg_lio": EXPENSIVE_AVG_LIO},
        ))
        metrics.append(_metric(
            "db.ase.top_statement_avg_lio", top_lio, "pages", "database",
            detail=_clip(cached[0]["sql"], 120) if cached else "",
        ))

    hit = read_cache_hit(session, system)
    if hit is not None:
        metrics.append(_metric("db.ase.data_cache_hit_pct", hit[0], "%", "database", detail=hit[1]))

    cpu = read_engine_cpu(session, system)
    if cpu is not None:
        metrics.append(_metric("db.ase.engine_cpu_pct", cpu[0], "%", "database", detail=cpu[1]))

    pools = read_memory_pools(session)
    if pools is not None:
        metrics.append(_metric(
            "db.ase.memory_pool_max_used_pct", pools[0], "%", "database",
            detail=pools[1], extra={"pools": pools[2]},
        ))

    return metrics


def collect_ase_metrics(system: str, cfg: dict) -> tuple[list[MetricResult], str | None]:
    """
    Opens ONE connection and returns (metrics, error). Mirrors
    collect_rfc_metrics so the orchestrator treats both identically.
    """
    params = build_ase_params(cfg)
    if params is None:
        return [], None
    with AseSession(system, params) as s:
        if not s.ok:
            return [], s.error
        try:
            metrics = build_metrics(system, s)
        except Exception as e:  # noqa: BLE001
            return [], f"ASE read failed: {type(e).__name__}: {e}"
    if not metrics:
        return [], "ASE connected but no MDA table was readable -- check 'enable monitoring' and mon_role"
    log.info(f"[{system}] ASE collector: {len(metrics)} metrics")
    return metrics, None


def collect_live(system: str, cfg: dict) -> dict:
    """Small, side-effect-free snapshot for the wall display, same shape
    philosophy as rfc_live: read, return, no evidence, no alerts."""
    metrics, err = collect_ase_metrics(system, cfg)
    return {
        "system": system,
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "error": err,
        "metrics": {m.name: {"value": m.value, "status": m.status.value, "detail": m.detail}
                    for m in metrics},
    }

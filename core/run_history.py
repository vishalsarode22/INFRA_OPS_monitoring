"""
Run history: every sweep and every scheduling decision, durably.

    dashboard/run_history.sqlite      (IBO_RUN_HISTORY_DB overrides)

One APPEND-ONLY table. A row is written when a run ends (success, failed,
stopped, hung, abandoned, skipped, parked) and for every scheduling decision
that did not start a run (missed slot, skipped because something else held
the desktop, parked, held during a manual run). Rows are never updated; old
rows are pruned after IBO_RUN_HISTORY_DAYS (default 180).

FIELDS
    run_id, system, profile_id,
    trigger      schedule | manual-system | manual-profile | manual-all |
                 new-system | retry | rca | catch-up | cli
    planned_at   the slot it was planned for (scheduled runs)
    started_at, ended_at, duration_s,
    result       success | failed | skipped | hung-killed | abandoned |
                 stopped-by-user | missed | parked
    failed_stage, failure_reason, attempt, attempts_max,
    screenshots_captured,
    next_run_at, next_run_reason   (schedule | retry in Ns | parked until cleared)
    unexpected   1 when a run started outside its schedule: anything that is
                 not a manual/new-system/retry/rca run and did not start on
                 (or within the grace window after) a planned slot.

The legacy dashboard/run_history.json (last 50 events, shown on the start
page) is imported once into this table and keeps being written by the
dashboard for the old start-page feed.

Never raises into a caller: history must not be the reason a sweep fails.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH: str | None = None            # tests may set this
LEGACY_JSON = os.path.join(_BASE, "dashboard", "run_history.json")

TRIGGERS = ("schedule", "manual-system", "manual-profile", "manual-all", "new-system",
            "retry", "rca", "catch-up", "cli")
RESULTS = ("success", "failed", "skipped", "hung-killed", "abandoned", "stopped-by-user",
           "missed", "parked")
EXPECTED_TRIGGERS = ("manual-system", "manual-profile", "manual-all", "new-system", "retry",
                     "rca", "cli")

COLUMNS = ("id", "run_id", "system", "profile_id", "trigger", "planned_at", "started_at",
           "ended_at", "duration_s", "result", "failed_stage", "failure_reason", "attempt",
           "attempts_max", "screenshots_captured", "next_run_at", "next_run_reason",
           "unexpected", "note", "recorded_at")

_DDL = """
CREATE TABLE IF NOT EXISTS run_history (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL,
    system               TEXT NOT NULL DEFAULT '',
    profile_id           TEXT,
    trigger              TEXT NOT NULL DEFAULT '',
    planned_at           TEXT,
    started_at           TEXT,
    ended_at             TEXT,
    duration_s           REAL,
    result               TEXT NOT NULL,
    failed_stage         TEXT,
    failure_reason       TEXT,
    attempt              INTEGER,
    attempts_max         INTEGER,
    screenshots_captured INTEGER,
    next_run_at          TEXT,
    next_run_reason      TEXT,
    unexpected           INTEGER NOT NULL DEFAULT 0,
    note                 TEXT,
    recorded_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rh_time ON run_history(recorded_at);
CREATE INDEX IF NOT EXISTS ix_rh_system ON run_history(system, recorded_at);
CREATE TABLE IF NOT EXISTS run_history_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_lock = threading.Lock()
_local = threading.local()
_ready: set = set()
_pytest_dir: dict = {}


def db_path() -> str:
    if DB_PATH:
        return DB_PATH
    env = (os.getenv("IBO_RUN_HISTORY_DB") or "").strip()
    if env:
        return env
    if "PYTEST_CURRENT_TEST" in os.environ:
        # A test run must never write the real history next to the code.
        d = _pytest_dir.get("dir")
        if d is None:
            d = _pytest_dir["dir"] = tempfile.mkdtemp(prefix="ibo_run_history_")
        return os.path.join(d, "run_history.sqlite")
    return os.path.join(_BASE, "dashboard", "run_history.sqlite")


def _conn() -> sqlite3.Connection:
    path = db_path()
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = _local.conns = {}
    c = cache.get(path)
    if c is not None:
        return c
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    c.execute("PRAGMA busy_timeout=10000")
    with _lock:
        if path not in _ready:
            c.executescript(_DDL)
            _ready.add(path)
            try:
                _migrate_legacy(c)
            except Exception as exc:  # noqa: BLE001
                log.warning(f"run history: legacy import skipped: {exc}")
    cache[path] = c
    return c


# ---------------------------------------------------------------- helpers

def _stamp(value) -> str | None:
    """ISO-8601 with the local UTC offset; strings are kept as they are."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value.isoformat(timespec="seconds")
    return str(value)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def is_unexpected(trigger: str | None, planned_at=None, started_at=None,
                  grace: timedelta | None = None) -> bool:
    """A run that did not start on its plan (manual & co. are expected)."""
    if trigger in EXPECTED_TRIGGERS:
        return False
    if trigger not in ("schedule", "catch-up"):
        return True                          # unknown origin: flag it
    p, s = _parse(_stamp(planned_at)), _parse(_stamp(started_at))
    if p is None or s is None:
        return True
    if grace is None:
        try:
            from core.schedules import grace as _grace
            grace = _grace()
        except Exception:  # noqa: BLE001
            grace = timedelta(hours=1)
    if trigger == "catch-up":
        return s < p                         # started after the slot by design
    return s < p - timedelta(minutes=1) or s - p > grace


# ---------------------------------------------------------------- writing

def record(**fields) -> dict:
    """Append one row. Unknown keys go into `note`. Never raises."""
    row = {k: None for k in COLUMNS if k != "id"}
    extra = {}
    for k, v in fields.items():
        if k in row:
            row[k] = v
        else:
            extra[k] = v
    row["run_id"] = row["run_id"] or new_run_id()
    row["system"] = str(row["system"] or "")
    row["trigger"] = str(row["trigger"] or "")
    row["result"] = str(row["result"] or "failed")
    for k in ("planned_at", "started_at", "ended_at", "next_run_at"):
        row[k] = _stamp(row[k])
    if row["duration_s"] is None and row["started_at"] and row["ended_at"]:
        s, e = _parse(row["started_at"]), _parse(row["ended_at"])
        if s and e:
            row["duration_s"] = round((e - s).total_seconds(), 1)
    if row["unexpected"] is None:
        row["unexpected"] = (is_unexpected(row["trigger"], row["planned_at"], row["started_at"])
                             if row["started_at"] else False)
    row["unexpected"] = 1 if row["unexpected"] else 0
    if extra:
        note = row.get("note")
        row["note"] = ((str(note) + "; ") if note else "") + json.dumps(extra, default=str)[:900]
    if row["failure_reason"]:
        row["failure_reason"] = str(row["failure_reason"])[:900]
    row["recorded_at"] = _stamp(datetime.now())
    try:
        cols = [k for k in COLUMNS if k != "id"]
        _conn().execute(f"INSERT INTO run_history({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [row[k] for k in cols])
        _maybe_prune()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"run history not written ({type(exc).__name__}: {exc}): {row.get('system')} "
                    f"{row.get('result')}")
    return row


def record_decision(system: str, result: str, trigger: str = "schedule", profile_id=None,
                    planned_at=None, reason: str = "", next_run_at=None,
                    next_run_reason: str | None = None, **extra) -> dict:
    """A scheduling decision that did not run a sweep (missed, skipped, parked)."""
    return record(system=system, result=result, trigger=trigger, profile_id=profile_id,
                  planned_at=planned_at, failure_reason=reason or None,
                  next_run_at=next_run_at, next_run_reason=next_run_reason,
                  unexpected=False, **extra)


def record_run(system_config: dict, trigger: str, started_at, ok: bool, error=None,
               planned_at=None, profile_id=None, next_run_at=None, next_run_reason=None,
               note: str | None = None) -> dict:
    """
    One finished run, from the outcome main.py leaves in
    system_config["_run_ctx"] (attempt, failed stage, reason, screenshots,
    result). Works without it too (a stubbed pipeline): then ok/error decide.
    """
    ctx = system_config.get("_run_ctx") if isinstance(system_config.get("_run_ctx"), dict) else {}
    result = ctx.get("result")
    if ok and result in (None, "failed"):
        result = "success"
    if not ok and result in (None, "success"):
        result = "failed"
    reason = ctx.get("failure_reason")
    if error is not None and not reason:
        reason = f"{type(error).__name__}: {error}"
    if result == "success":
        reason = None
    if ctx.get("report") and result == "success":
        note = ((note + "; ") if note else "") + f"report {ctx['report']}"
    nra = ctx.get("next_run_at") or next_run_at
    nrr = ctx.get("next_run_reason") or next_run_reason
    return record(run_id=ctx.get("run_id"), system=system_config.get("name", ""),
                  profile_id=profile_id, trigger=trigger, planned_at=planned_at,
                  started_at=started_at, ended_at=datetime.now(), result=result,
                  failed_stage=ctx.get("failed_stage") if result != "success" else None,
                  failure_reason=reason, attempt=ctx.get("attempt"),
                  attempts_max=ctx.get("attempts_max"),
                  screenshots_captured=ctx.get("screenshots_captured"),
                  next_run_at=nra, next_run_reason=nrr, note=note)


_prune = {"at": None}


def _maybe_prune() -> None:
    now = datetime.now()
    if _prune["at"] and now - _prune["at"] < timedelta(hours=6):
        return
    _prune["at"] = now
    try:
        days = max(7, int(os.getenv("IBO_RUN_HISTORY_DAYS", "180") or 180))
    except ValueError:
        days = 180
    cutoff = _stamp(now - timedelta(days=days))
    try:
        _conn().execute("DELETE FROM run_history WHERE recorded_at < ?", (cutoff,))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"run history prune skipped: {exc}")


def _migrate_legacy(c: sqlite3.Connection) -> None:
    """Import dashboard/run_history.json once (legacy start-page feed)."""
    done = c.execute("SELECT value FROM run_history_meta WHERE key='legacy_imported'").fetchone()
    if done:
        return
    c.execute("INSERT OR REPLACE INTO run_history_meta(key, value) VALUES ('legacy_imported', ?)",
              (_stamp(datetime.now()),))
    if DB_PATH is None and "PYTEST_CURRENT_TEST" in os.environ and not os.getenv("IBO_RUN_HISTORY_DB"):
        return                                  # never import the real file into a test DB
    try:
        from core.atomic_json import read_json
        events = read_json(LEGACY_JSON, default=None)
    except Exception:  # noqa: BLE001
        events = None
    if not isinstance(events, list):
        return
    status_map = {"ok": "success", "error": "failed", "stopped": "stopped-by-user"}
    n = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "skipped":
            system, result, when = ev.get("requested") or "", "skipped", ev.get("at")
            reason = f"{ev.get('blocked_by') or 'another run'} was still running"
        else:
            system, when = ev.get("system") or "", ev.get("timestamp")
            result = status_map.get(str(ev.get("status") or ""), "failed")
            reason = ev.get("event") if result != "success" else None
        stamp = _stamp(_parse(when)) or _stamp(datetime.now())
        c.execute("INSERT INTO run_history(run_id, system, trigger, ended_at, result, failure_reason, "
                  "unexpected, note, recorded_at) VALUES (?,?,?,?,?,?,0,?,?)",
                  (f"legacy-{n}", system, "legacy", stamp, result, reason,
                   "imported from run_history.json", stamp))
        n += 1
    if n:
        log.info(f"run history: imported {n} event(s) from run_history.json")


# ---------------------------------------------------------------- reading

_EVENT_TEXT = {"success": "Sweep completed", "failed": "Sweep failed", "skipped": "Run skipped",
               "hung-killed": "Sweep hung (SAP killed)", "abandoned": "Sweep hung (thread abandoned)",
               "stopped-by-user": "Stopped from the dashboard", "missed": "Scheduled slot missed",
               "parked": "Parked (system down)"}
_LEGACY_STATUS = {"success": "ok", "stopped-by-user": "stopped", "skipped": "skipped",
                  "missed": "skipped", "parked": "skipped"}


def _decorate(row: dict) -> dict:
    """Add the fields the old start-page feed reads (event, status, timestamp)."""
    out = dict(row)
    out["unexpected"] = bool(out.get("unexpected"))
    text = _EVENT_TEXT.get(out.get("result"), out.get("result") or "")
    if out.get("failure_reason") and out.get("result") != "success":
        text += f": {str(out['failure_reason'])[:120]}"
    out["event"] = text
    out["status"] = _LEGACY_STATUS.get(out.get("result"), "error")
    when = _parse(out.get("ended_at") or out.get("started_at") or out.get("recorded_at"))
    out["timestamp"] = when.strftime("%Y-%m-%d %H:%M:%S") if when else None
    return out


def query(system: str = "", result: str = "", since=None, limit: int = 100,
          trigger: str = "") -> list[dict]:
    """Newest first."""
    sql = "SELECT * FROM run_history WHERE 1=1"
    args: list = []
    if system:
        sql += " AND system = ?"
        args.append(system)
    if result:
        results = [r.strip() for r in str(result).split(",") if r.strip()]
        sql += " AND result IN (%s)" % ",".join("?" * len(results))
        args += results
    if trigger:
        sql += " AND trigger = ?"
        args.append(trigger)
    since_dt = since if isinstance(since, datetime) else _parse(since)
    if since_dt is not None:
        # In SQL (indexed), not only in Python while scanning every row --
        # perf test 28.10.2026. The Python check below stays for odd stamps.
        sql += " AND recorded_at >= ?"
        args.append((since_dt - timedelta(days=1)).strftime("%Y-%m-%d"))   # stamps carry offsets
    rows = []
    try:
        limit = max(1, min(int(limit or 100), 5000))
    except (TypeError, ValueError):
        limit = 100
    try:
        cur = _conn().execute(sql + " ORDER BY id DESC", args)
        for r in cur:
            d = dict(r)
            if since_dt is not None:
                when = _parse(d.get("recorded_at"))
                if when is not None and when < since_dt:
                    continue
            rows.append(_decorate(d))
            if len(rows) >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        log.warning(f"run history not readable: {exc}")
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = [c for c in COLUMNS if c != "id"]
    writer.writerow(cols)
    for r in rows:
        line = []
        for c in cols:
            v = r.get(c)
            if c == "unexpected":
                v = 1 if v else 0
            text = "" if v is None else str(v)
            if text[:1] in ("=", "+", "-", "@") and c not in ("duration_s",):
                text = "'" + text                    # no formula injection in Excel
            line.append(text)
        writer.writerow(line)
    return buf.getvalue()

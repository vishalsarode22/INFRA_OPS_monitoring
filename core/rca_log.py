"""
RCA log: one row per performance analysis -- when it ran, why, what it found
as the cause, who or what was causing it, and who got the mail.

Written by evaluation/rca_pipeline.run_rca (every SAP GUI RCA, automatic or
from the Performance monitoring button or a sweep), by the dashboard when an
RCA could not start, and by the AI RCA page's "Analyse now". Shown on the
AI RCA page (GET /api/rca/log).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS rca_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    system      TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT '',      -- automatic | manual | sweep | analyse-now
    reason      TEXT NOT NULL DEFAULT '',      -- why it ran (the trigger's own words)
    result      TEXT NOT NULL DEFAULT 'running', -- running | done | failed | stopped | not-started
    severity    TEXT NOT NULL DEFAULT '',
    headline    TEXT NOT NULL DEFAULT '',
    root_cause  TEXT NOT NULL DEFAULT '',
    culprits    TEXT NOT NULL DEFAULT '[]',    -- [{type, name, evidence, impact}]
    mailed_to   TEXT NOT NULL DEFAULT '',
    mail_result TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',      -- model / rules-based
    report      TEXT NOT NULL DEFAULT '',      -- PDF path
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS rca_runs_started ON rca_runs(started_at);
"""
_ready: set = set()
_lock = threading.Lock()
_FMT = "%Y-%m-%d %H:%M:%S"


def _db():
    from core import ops_db, store
    c = ops_db.db()
    if store.DB_PATH not in _ready:
        with _lock:
            if store.DB_PATH not in _ready:
                c.executescript(_DDL)
                _ready.add(store.DB_PATH)
    return c


def trigger_of(reason: str) -> str:
    r = str(reason or "").lower()
    if r.startswith("manual"):
        return "manual"
    if r.startswith("sweep") or "during the sweep" in r:
        return "sweep"
    if r.startswith("analyse"):
        return "analyse-now"
    return "automatic"


def start(system: str, reason: str, trigger: str = "") -> int:
    """A row for an RCA that is starting; returns its id (0 if it could not be written)."""
    try:
        cur = _db().execute(
            "INSERT INTO rca_runs(started_at, system, trigger, reason) VALUES (?,?,?,?)",
            (datetime.now().strftime(_FMT), system, trigger or trigger_of(reason), str(reason or "")[:500]))
        return int(cur.lastrowid or 0)
    except Exception as exc:  # noqa: BLE001 -- the log must never stop an RCA
        log.debug(f"rca log start not written: {exc}")
        return 0


def finish(run_id: int, result: str, analysis: dict | None = None, mailed_to=None,
           mail_result: str = "", report: str = "", error: str = "") -> None:
    if not run_id:
        return
    a = analysis or {}
    try:
        from core import ops_db
        culprits = [{k: str(c.get(k) or "")[:300] for k in ("type", "name", "evidence", "impact")}
                    for c in (a.get("culprits") or []) if isinstance(c, dict)][:8]
        _db().execute(
            "UPDATE rca_runs SET ended_at=?, result=?, severity=?, headline=?, root_cause=?, culprits=?, "
            "mailed_to=?, mail_result=?, source=?, report=?, error=? WHERE id=?",
            (datetime.now().strftime(_FMT), result, str(a.get("severity") or "")[:20],
             str(a.get("headline") or "")[:400], str(a.get("root_cause") or "")[:1200],
             ops_db.dumps(culprits), ", ".join(mailed_to or []) if not isinstance(mailed_to, str)
             else mailed_to, str(mail_result or "")[:300], str(a.get("source") or "")[:60],
             str(report or "")[:400], str(error or "")[:600], run_id))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"rca log finish not written: {exc}")


def not_started(system: str, reason: str, why: str) -> None:
    """An RCA that was asked for but did not run (busy, paused, SAP GUI held...)."""
    run_id = start(system, reason)
    finish(run_id, "not-started", error=why)


def query(days: int = 7, system: str | None = None, limit: int = 300) -> list[dict]:
    from core import ops_db
    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime(_FMT)
    sql = "SELECT * FROM rca_runs WHERE started_at >= ?"
    args: list = [since]
    if system:
        sql += " AND system = ?"
        args.append(system)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in _db().execute(sql, args).fetchall():
        d = dict(r)
        d["culprits"] = ops_db.loads(d.get("culprits"), []) or []
        out.append(d)
    return out

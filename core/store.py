"""
One database for everything the services write at run time.

    dashboard/ibops.sqlite   (SQLite, WAL mode, busy timeout)

Tables
    documents   every former JSON state file, keyed by its old relative path
                (dashboard/snapshots/PS4.json, dashboard/events/PS4.json, ...)
    lines       every former JSONL file (metric history), one row per line
    jobs        work for the job runner: mail, AI analysis, maintenance
    heartbeats  one row per service (web, collector, jobs) touched every 30 s
    alerts      the alert ledger (core/alert_ledger.py creates it here too)
    users       dashboard logins: role and customer scope
    sessions    login cookies

WHY ONE FILE
SQLite in WAL mode gives readers and writers real isolation across the
three processes (web, collector, jobs) without the lock-file dance of
core/atomic_json.py. Backups are one file. Migration is one schema
number, below.

HOW THE OLD CODE KEEPS WORKING
core/atomic_json.read_json / write_json / append_line take the same path
arguments as before and, with IBO_STATE_BACKEND=sqlite (the default), go
to the `documents` / `lines` tables keyed on that path. The first read of
a path that is not in the database yet but exists on disk imports the
file, so nothing has to be migrated by hand. IBO_STATE_BACKEND=files
restores the old behaviour.

What stays on disk: configuration (systems.yaml, profiles, thresholds,
.env), reports and screenshots, logs. Those are files on purpose.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Iterator

from utils.logger import get_logger

log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("IBO_DB_PATH") or os.path.join(BASE_DIR, "dashboard", "ibops.sqlite")
SCHEMA_VERSION = 2

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS documents (
    path        TEXT PRIMARY KEY,
    body        TEXT NOT NULL,
    schema      INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lines (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    path  TEXT NOT NULL,
    ts    TEXT NOT NULL,
    line  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lines_path ON lines(path, id);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_at      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    worker      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_due ON jobs(status, run_at);
CREATE TABLE IF NOT EXISTS heartbeats (
    service  TEXT PRIMARY KEY,
    host     TEXT NOT NULL,
    pid      INTEGER NOT NULL,
    beat_at  TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL,                     -- admin | operator | viewer
    customers   TEXT NOT NULL DEFAULT '*',         -- '*' or comma list
    disabled    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    remote      TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn() -> sqlite3.Connection:
    """One connection per thread, WAL, autocommit, 10 s busy wait."""
    global _initialised
    c = getattr(_local, "conn", None)
    if c is not None:
        return c
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=10000")
    with _init_lock:
        if not _initialised:
            c.executescript(_DDL)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)",
                      (str(SCHEMA_VERSION),))
            _initialised = True
    _local.conn = c
    return c


def rel(path: str) -> str:
    """Key a document by its path relative to the project root, forward slashes."""
    p = os.path.abspath(path)
    try:
        p = os.path.relpath(p, BASE_DIR)
    except ValueError:
        pass
    return p.replace("\\", "/")


# ---------------------------------------------------------------- documents

def get_document(path: str, default: Any = None) -> Any:
    row = conn().execute("SELECT body FROM documents WHERE path=?", (rel(path),)).fetchone()
    if row is None:
        # Lazy migration: first touch of a path imports the file it replaces.
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                put_document(path, data)
                log.info(f"store: imported {rel(path)} from disk")
                return data
            except Exception as exc:
                log.warning(f"store: could not import {rel(path)}: {exc}")
        return default
    try:
        return json.loads(row["body"])
    except ValueError:
        return default


def put_document(path: str, data: Any) -> None:
    body = json.dumps(data, default=str, separators=(",", ":"))
    conn().execute(
        "INSERT INTO documents(path, body, schema, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET body=excluded.body, schema=excluded.schema, "
        "updated_at=excluded.updated_at", (rel(path), body, SCHEMA_VERSION, _now()))


def delete_document(path: str) -> None:
    conn().execute("DELETE FROM documents WHERE path=?", (rel(path),))


def list_documents(prefix: str) -> list[str]:
    """Paths (relative) under a directory prefix, merged with files on disk."""
    key = rel(prefix).rstrip("/") + "/"
    rows = conn().execute("SELECT path FROM documents WHERE path LIKE ?", (key + "%",)).fetchall()
    found = {r["path"] for r in rows}
    if os.path.isdir(prefix):
        for f in os.listdir(prefix):
            if f.endswith(".json"):
                found.add(key + f)
    return sorted(found)


def document_mtime(path: str) -> str | None:
    row = conn().execute("SELECT updated_at FROM documents WHERE path=?", (rel(path),)).fetchone()
    return row["updated_at"] if row else None


# -------------------------------------------------------------------- lines

def append_lines(path: str, lines: list[str]) -> None:
    now = _now()
    conn().executemany("INSERT INTO lines(path, ts, line) VALUES (?,?,?)",
                       [(rel(path), now, ln.rstrip("\n")) for ln in lines if ln.strip()])


def read_lines(path: str) -> Iterator[str]:
    key = rel(path)
    c = conn()
    exists = c.execute("SELECT 1 FROM lines WHERE path=? LIMIT 1", (key,)).fetchone()
    if exists is None and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                imported = [ln for ln in fh]
            append_lines(path, imported)
            log.info(f"store: imported {len(imported)} line(s) from {key}")
        except Exception as exc:
            log.warning(f"store: could not import {key}: {exc}")
    for row in c.execute("SELECT line FROM lines WHERE path=? ORDER BY id", (key,)):
        yield row["line"]


def prune_lines(days: int = 400) -> int:
    """Delete stored history lines older than `days` (the table had no retention)."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return conn().execute("DELETE FROM lines WHERE ts < ?", (cutoff,)).rowcount


def delete_lines(path: str) -> int:
    return conn().execute("DELETE FROM lines WHERE path=?", (rel(path),)).rowcount


# --------------------------------------------------------------------- jobs

def enqueue(kind: str, payload: dict, delay_seconds: float = 0, max_attempts: int = 3) -> int:
    run_at = (datetime.now() + timedelta(seconds=delay_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn().execute(
        "INSERT INTO jobs(kind, payload, run_at, created_at, max_attempts) VALUES (?,?,?,?,?)",
        (kind, json.dumps(payload, default=str), run_at, _now(), max_attempts))
    return int(cur.lastrowid)


def claim_job(worker: str, kinds: list[str] | None = None) -> dict | None:
    """Atomically take the next due job. None when nothing is due."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        sql = "SELECT * FROM jobs WHERE status='queued' AND run_at<=? "
        args: list = [_now()]
        if kinds:
            sql += "AND kind IN (%s) " % ",".join("?" * len(kinds))
            args += kinds
        sql += "ORDER BY id LIMIT 1"
        row = c.execute(sql, args).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, worker=? "
                  "WHERE id=?", (_now(), worker, row["id"]))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    job = dict(row)
    job["payload"] = json.loads(job["payload"])
    job["attempts"] += 1
    return job


def finish_job(job_id: int, ok: bool, error: str = "", retry_in: float = 300) -> None:
    c = conn()
    row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    if ok:
        c.execute("UPDATE jobs SET status='done', finished_at=?, error='' WHERE id=?", (_now(), job_id))
    elif row and row["attempts"] < row["max_attempts"]:
        run_at = (datetime.now() + timedelta(seconds=retry_in)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE jobs SET status='queued', run_at=?, error=? WHERE id=?",
                  (run_at, error[:500], job_id))
    else:
        c.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                  (_now(), error[:500], job_id))


def requeue_stale_running(max_age_seconds: float = 1800) -> int:
    """A job runner that died mid-job leaves 'running' rows; give them back."""
    cutoff = (datetime.now() - timedelta(seconds=max_age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    return conn().execute("UPDATE jobs SET status='queued' WHERE status='running' AND started_at<?",
                          (cutoff,)).rowcount


def jobs_summary() -> dict:
    out: dict = {}
    for r in conn().execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        out[r["status"]] = r["n"]
    return out


def recent_jobs(limit: int = 50) -> list[dict]:
    rows = conn().execute("SELECT id, kind, status, attempts, run_at, created_at, finished_at, "
                          "worker, error FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def prune_jobs(days: int = 14) -> int:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return conn().execute("DELETE FROM jobs WHERE status IN ('done','failed') AND created_at<?",
                          (cutoff,)).rowcount


# --------------------------------------------------------------- heartbeats

def beat(service: str, detail: str = "") -> None:
    import socket
    conn().execute(
        "INSERT INTO heartbeats(service, host, pid, beat_at, detail) VALUES (?,?,?,?,?) "
        "ON CONFLICT(service) DO UPDATE SET host=excluded.host, pid=excluded.pid, "
        "beat_at=excluded.beat_at, detail=excluded.detail",
        (service, socket.gethostname(), os.getpid(), _now(), detail[:200]))


def heartbeats() -> list[dict]:
    now = datetime.now()
    out = []
    for r in conn().execute("SELECT * FROM heartbeats ORDER BY service"):
        d = dict(r)
        try:
            age = (now - datetime.strptime(d["beat_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        except ValueError:
            age = None
        d["age_seconds"] = None if age is None else int(age)
        out.append(d)
    return out


# ------------------------------------------------------------------- backup

def backup(dest_path: str) -> str:
    """Consistent copy of the live database (SQLite online backup API)."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    dst = sqlite3.connect(dest_path)
    try:
        conn().backup(dst)
    finally:
        dst.close()
    return dest_path

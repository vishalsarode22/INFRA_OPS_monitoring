"""
Tables for the operations features, in the same database as everything else
(dashboard/ibops.sqlite, core/store.py).

    maintenance      planned windows: no alerts, no sweeps, grey on the wall
    silences         snooze / ignore-until-fixed / acknowledge, per alert
    parked           systems whose scheduled sweeps are held until cleared
    timeline         one row per thing that happened to a system
    oncall_overrides "Om covers 10-12 Oct" on top of the weekly rotation
    tickets          tickets opened for red alerts, and their state
    kb               knowledge base: how a problem was fixed last time
    runbook_ticks    who ticked which runbook step, when
    sap_actions      one-click actions: requested, approved, result
    action_tokens    single-use tokens behind the links in alert mails

Created on first use; nothing to migrate by hand. All times are local
"YYYY-MM-DD HH:MM:SS" strings, like the rest of the store.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from core import store

_ready: set = set()          # database paths whose tables exist
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS maintenance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    system       TEXT NOT NULL,              -- a system name, or '*' for all
    starts_at    TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_maint_sys ON maintenance(system, ends_at);
CREATE TABLE IF NOT EXISTS silences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    system      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '*',   -- alert kind, or '*' for every kind
    mode        TEXT NOT NULL,               -- snooze | until_fixed | ack
    until       TEXT,                        -- snooze end; NULL for until_fixed / ack
    reason      TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    cleared_at  TEXT,
    cleared_by  TEXT
);
CREATE INDEX IF NOT EXISTS ix_sil_sys ON silences(system, cleared_at);
CREATE TABLE IF NOT EXISTS parked (
    system     TEXT PRIMARY KEY,
    reason     TEXT NOT NULL DEFAULT '',
    parked_by  TEXT NOT NULL DEFAULT '',
    parked_at  TEXT NOT NULL,
    auto       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS timeline (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    system    TEXT NOT NULL,
    category  TEXT NOT NULL,                 -- alert | state | sweep | vpn | maintenance | ...
    title     TEXT NOT NULL,
    detail    TEXT NOT NULL DEFAULT '',
    actor     TEXT NOT NULL DEFAULT '',
    severity  TEXT NOT NULL DEFAULT 'info',  -- info | ok | warning | critical
    ref       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_tl_sys ON timeline(system, at);
CREATE INDEX IF NOT EXISTS ix_tl_at ON timeline(at);
CREATE TABLE IF NOT EXISTS oncall_overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    emails      TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    system       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL DEFAULT '',
    provider     TEXT NOT NULL,
    external_id  TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'opening', -- opening | open | closed | failed
    subject      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    closed_at    TEXT,
    closed_by    TEXT,
    resolution   TEXT NOT NULL DEFAULT '',
    last_sync    TEXT,
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tk_open ON tickets(status, system, kind);
CREATE TABLE IF NOT EXISTS kb (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,               -- space-separated normalised tokens
    rule        TEXT NOT NULL DEFAULT '',    -- alert kind / correlation rule / metric
    system      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL,
    fix         TEXT NOT NULL,
    actions     TEXT NOT NULL DEFAULT '[]',
    root_cause  TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual', -- manual | incident | ticket
    source_ref  TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    uses        INTEGER NOT NULL DEFAULT 0,
    retired     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runbook_ticks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key    TEXT NOT NULL,                -- runbook id + system + day
    step       INTEGER NOT NULL,
    done       INTEGER NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rb_run ON runbook_ticks(run_key, step, id);
CREATE TABLE IF NOT EXISTS sap_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    system        TEXT NOT NULL,
    action        TEXT NOT NULL,
    params        TEXT NOT NULL DEFAULT '{}',
    reason        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | failed | rejected | expired
    requested_by  TEXT NOT NULL DEFAULT '',
    requested_at  TEXT NOT NULL,
    approved_by   TEXT,
    approved_at   TEXT,
    finished_at   TEXT,
    preview       TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS action_approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    system      TEXT NOT NULL,
    action      TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT,
    revoked_by  TEXT
);
CREATE TABLE IF NOT EXISTS extra_check_results (
    system      TEXT PRIMARY KEY,
    at          TEXT,
    payload     TEXT NOT NULL DEFAULT '[]',
    claimed_at  TEXT
);
CREATE TABLE IF NOT EXISTS sap_clock (
    system      TEXT PRIMARY KEY,
    offset_s    REAL NOT NULL,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_attempts (
    scope       TEXT PRIMARY KEY,
    tries       INTEGER NOT NULL DEFAULT 0,
    last_at     TEXT NOT NULL,
    last_ok     INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alert_state (
    system          TEXT NOT NULL,
    skey            TEXT NOT NULL,
    status          TEXT NOT NULL,
    since           TEXT NOT NULL,
    last_sent_at    TEXT,
    last_sent_level TEXT,
    escalated_at    TEXT,
    delivered       INTEGER NOT NULL DEFAULT 0,
    value           TEXT NOT NULL DEFAULT '',
    teams           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (system, skey)
);
CREATE TABLE IF NOT EXISTS alert_sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    system      TEXT NOT NULL,
    metric      TEXT NOT NULL,
    instance    TEXT NOT NULL DEFAULT '',
    label       TEXT NOT NULL DEFAULT '',
    level       TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',
    limits      TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    recipients  TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT '',
    batch       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS alert_sends_sys ON alert_sends(system, metric, at);
CREATE INDEX IF NOT EXISTS alert_sends_at ON alert_sends(at);
CREATE TABLE IF NOT EXISTS alert_live_seen (
    system  TEXT NOT NULL,
    key     TEXT NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (system, key)
);
CREATE TABLE IF NOT EXISTS custom_seen (
    system      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ident       TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    counted     INTEGER NOT NULL DEFAULT 1,
    info        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (system, kind, ident)
);
CREATE TABLE IF NOT EXISTS job_user_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    system      TEXT NOT NULL,
    user        TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    jobs        TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting',
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS report_retries (
    system      TEXT PRIMARY KEY,
    attempt     INTEGER NOT NULL,
    due_at      TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    first_at    TEXT NOT NULL,
    started_at  TEXT
);
CREATE TABLE IF NOT EXISTS action_tokens (
    nonce     TEXT PRIMARY KEY,
    used_at   TEXT NOT NULL,
    action    TEXT NOT NULL DEFAULT '',
    system    TEXT NOT NULL DEFAULT ''
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(str(stamp).strip()[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(stamp).strip()).replace(tzinfo=None)
    except ValueError:
        return None


def fmt(when: datetime | None) -> str | None:
    return when.strftime("%Y-%m-%d %H:%M:%S") if when else None


# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS does
# not add them to a table that already exists.
_LATE_COLUMNS = (
    ("alert_state", "delivered", "INTEGER NOT NULL DEFAULT 0"),
    ("alert_state", "value", "TEXT NOT NULL DEFAULT ''"),
    ("alert_sends", "batch", "TEXT NOT NULL DEFAULT ''"),
    ("alert_state", "teams", "TEXT NOT NULL DEFAULT ''"),
    ("report_retries", "started_at", "TEXT"),
    # the profile whose run was held: its retry keeps that profile's delivery,
    # recipients and T-code set (review 28.10.2026)
    ("report_retries", "profile_id", "TEXT"),
    # per-value silences from an alert mail: the level the mail was about
    # (a rise from WARNING to CRITICAL still alerts) and the value's name
    ("silences", "level", "TEXT NOT NULL DEFAULT ''"),
    ("silences", "label", "TEXT NOT NULL DEFAULT ''"),
    # hysteresis and grading (core/alert_engine.py): consecutive breaching /
    # clear readings, whether the alert is raised, its colour, and when it
    # was last raised (a second breach inside the window goes to the manager)
    ("alert_state", "breach_n", "INTEGER NOT NULL DEFAULT 0"),
    ("alert_state", "breach_since", "TEXT"),
    ("alert_state", "clear_n", "INTEGER NOT NULL DEFAULT 0"),
    ("alert_state", "raised", "INTEGER NOT NULL DEFAULT 0"),
    ("alert_state", "raised_at", "TEXT"),
    ("alert_state", "grade", "TEXT NOT NULL DEFAULT ''"),
    ("alert_state", "last_raised_at", "TEXT"),
)


def _add_missing_columns(c) -> None:
    for table, col, decl in _LATE_COLUMNS:
        try:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if have and col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                c.commit()
        except Exception:  # noqa: BLE001
            pass


def db():
    """The shared connection (core/store.py), with these tables created once."""
    c = store.conn()
    key = store.DB_PATH
    if key not in _ready:
        with _lock:
            if key not in _ready:
                c.executescript(_DDL)
                # The mail ledger's table (core/alert_ledger.py) lives in the same
                # file but was only created on the first mail decision, so pages
                # that read it (Noise, insights) crashed on a fresh install.
                from core.alert_ledger import _DDL as _LEDGER_DDL
                c.executescript(_LEDGER_DDL)
                _add_missing_columns(c)
                _ready.add(key)
    return c


def rows(sql: str, args=()) -> list[dict]:
    return [dict(r) for r in db().execute(sql, args).fetchall()]


def one(sql: str, args=()) -> dict | None:
    r = db().execute(sql, args).fetchone()
    return dict(r) if r else None


def execute(sql: str, args=()) -> int:
    cur = db().execute(sql, args)
    return int(cur.lastrowid or 0) if sql.lstrip().upper().startswith("INSERT") else int(cur.rowcount)


def dumps(value) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def reset_for_tests() -> None:
    """Tests point IBO_DB_PATH somewhere new; forget that the DDL ran."""
    _ready.clear()

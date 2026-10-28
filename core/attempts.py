"""
Back-off for outgoing SMS, voice calls and WhatsApp messages.

The alert ledger lets a FAILED mail be tried again at the next check (a
mail that did not go out has not told anybody). For paid, intrusive
channels that is wrong: a rejected SMS template or a bad number would be
retried every minute for the whole outage. Each send is therefore keyed by
a scope ("esc:PS4:escalation_2:system_down:2026-10-07 08:40:00") and:

    allowed(scope, delays)   True when the next try is due: tries are spaced
                             by `delays` (seconds after the previous try);
                             after the last delay no more tries
    done(scope, ok, error)   record the try
    last(scope)              the stored row (tries, last_at, last_ok, last_error)

A scope that succeeded is not tried again by allowed() unless
`repeat_after_s` has passed (used for WhatsApp: at most one message per
alert per interval, whether it got through or not).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core import ops_db

DEFAULT_DELAYS = (0, 300, 900, 3600)          # now, +5 min, +15 min, +60 min -- then give up


def last(scope: str) -> dict | None:
    try:
        return ops_db.one("SELECT * FROM send_attempts WHERE scope=?", (scope,))
    except Exception:
        return None


def allowed(scope: str, delays=DEFAULT_DELAYS, repeat_after_s: float | None = None) -> bool:
    row = last(scope)
    if row is None:
        return True
    at = ops_db.parse(row["last_at"]) or datetime.min
    age = (datetime.now() - at).total_seconds()
    if repeat_after_s is not None:
        return age >= repeat_after_s
    if row["last_ok"]:
        return False
    tries = int(row["tries"] or 0)
    if tries >= len(delays):
        return False
    return age >= delays[tries]


def gave_up(scope: str, delays=DEFAULT_DELAYS) -> bool:
    row = last(scope)
    return bool(row) and not row["last_ok"] and int(row["tries"] or 0) >= len(delays)


def done(scope: str, ok: bool, error: str = "") -> None:
    try:
        ops_db.execute(
            "INSERT INTO send_attempts(scope, tries, last_at, last_ok, last_error) VALUES (?,1,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET tries=tries+1, last_at=excluded.last_at, "
            "last_ok=excluded.last_ok, last_error=excluded.last_error",
            (scope, ops_db.now(), int(bool(ok)), str(error or "")[:300]))
    except Exception:
        pass


def prune(days: int = 30) -> int:
    try:
        cut = ops_db.fmt(datetime.now() - timedelta(days=days))
        return ops_db.execute("DELETE FROM send_attempts WHERE last_at < ?", (cut,))
    except Exception:
        return 0

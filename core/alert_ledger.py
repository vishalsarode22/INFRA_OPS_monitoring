"""
Every alert this system sends, in one table, with one suppression rule.

Before this, each sender decided on its own whether to mail again: the
critical alert went out every sweep, the T-code failure mail every sweep,
the failure alert every attempt, job-owner and dump-owner mail once a day
each by their own bookkeeping, and the planned DOWN reminder every five
minutes. Nothing could answer "what did we already send about PS4 today",
and a mailbox that gets the same message every two hours gets a filter
pointed at it within a week.

Now a sender asks first:

    ok, why = ledger.should_send(system, kind, key, min_interval_s=3600)

`kind` is the alert type (critical, tcode_failure, sweep_failure, down,
vpn_failed, credentials, escalation, user_dumps, ...). `key` is what makes
this instance the same as the last one -- for a critical alert, the sorted
list of critical metrics; for a T-code failure, the sorted T-codes; for
DOWN, the system. The same (system, kind, key) is not re-sent inside
min_interval_s, and a daily cap per (system, kind) stops any sender from
flooding. A CHANGED key (one more metric critical) sends immediately: the
situation changed, which is exactly what a person wants to hear about.

Suppressed sends are recorded too (suppressed=1), so the dashboard can
show "critical alert for PS4 held back, same as 14:10", not silence.

Storage: dashboard/alerts.sqlite (SQLite, WAL mode -- safe across the
dashboard and main.py processes without the JSON-file locking dance).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Same file as core/store.py: one database, one backup, one schema.
from core.store import DB_PATH  # noqa: E402
_lock = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at     TEXT NOT NULL,
    system      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL DEFAULT '',
    severity    TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    recipients  TEXT NOT NULL DEFAULT '',
    suppressed  INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_alerts_lookup ON alerts(system, kind, key, sent_at);
CREATE INDEX IF NOT EXISTS ix_alerts_day ON alerts(sent_at);
"""


def _connect() -> sqlite3.Connection:
    # Read store.DB_PATH at call time: a copy taken at import kept writing to
    # the real database after a test (or IBO_DB_PATH) pointed the store elsewhere.
    from core import store as _store
    path = _store.DB_PATH or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    return conn


def _default_cap(kind: str) -> int:
    try:
        return int(os.getenv(f"IBO_ALERT_DAILY_CAP_{kind.upper()}",
                             os.getenv("IBO_ALERT_DAILY_CAP", "24")) or 24)
    except ValueError:
        return 24


def _norm_key(key) -> str:
    if key is None:
        return ""
    if isinstance(key, (list, tuple, set)):
        return ",".join(sorted(str(k) for k in key))
    return str(key)


# ------------------------------------------------------------------ the gate
#
# ONE gate every alert / notification mail passes through (mailer.send_html,
# send_html_now with an interval, deliver(); the per-value engine calls it
# directly). In order:
#   1. policy      kinds the owner switched off (IBO_DISABLED_MAIL_KINDS,
#                  default "sweep_failure,report_held": no "pipeline failed
#                  (attempt n/2)" and no "REPORT HELD" mails)
#   2. silences    maintenance / snooze / acknowledge (core/alert_control)
#   3. mail outage the sending account is suspended after a quota answer
#                  (notifications/mailer.py) -- non-critical mail is dropped
#                  with a record; CRITICAL / SYSTEM DOWN go on (secondary
#                  account, WhatsApp)
#   4. dedup       same (system, kind, key) inside min_interval_s
#   5. caps        per (system, kind) per day (IBO_ALERT_DAILY_CAP[_<KIND>]),
#                  per system (IBO_MAIL_CAP_PER_SYSTEM, default 40) and in
#                  total (IBO_MAIL_CAP_TOTAL, default 300) over a rolling
#                  24 h -- well below Gmail's 500/day
# Every decision is written to the ledger and, so people can see it, to the
# Alert history page (alert_sends) with its reason: sent, suppressed-policy,
# suppressed-silenced, suppressed-mail-outage, suppressed-dedup,
# suppressed-cap, send failed.

def disabled_kinds() -> set[str]:
    raw = os.getenv("IBO_DISABLED_MAIL_KINDS", "sweep_failure,report_held")
    return {k.strip() for k in str(raw or "").split(",") if k.strip()}


def _graph() -> bool:
    try:
        from notifications import graph_mail
        return graph_mail.enabled()
    except Exception:  # noqa: BLE001
        return False


def caps_on() -> bool:
    """
    Mail limits (per kind a day, per system, in total, the CRITICAL flood
    guard) are OFF by default since 28.10.2026: CEAT S4P alerts were held at
    the 200-a-day cap. The same alert is still not repeated within its
    repeat interval (dedup) -- that is not a limit. IBO_MAIL_CAPS=1 turns the
    limits back on.
    """
    return os.getenv("IBO_MAIL_CAPS", "0").strip().lower() in ("1", "true", "yes", "on")


def cap_per_system(system: str | None = None) -> int:
    """
    Everyday mails per system in 24 h: the system's own setting (Systems page,
    alert_timing.mail_cap_per_day), else IBO_MAIL_CAP_PER_SYSTEM, else 40 --
    150 when mail goes through Microsoft 365 (Graph), whose limit is 10,000
    recipients a day instead of Gmail's 500 mails.
    """
    if system:
        try:
            from core import alert_timing
            own = alert_timing.system_value(system.split(":", 1)[1] if system.startswith("network:")
                                            else system, "mail_cap_per_day")
            if own is not None:
                return max(0, own)
        except Exception:  # noqa: BLE001
            pass
    default = 150 if _graph() else 40
    try:
        return max(0, int(os.getenv("IBO_MAIL_CAP_PER_SYSTEM", str(default)) or default))
    except ValueError:
        return default


def cap_total() -> int:
    default = 1500 if _graph() else 300
    try:
        return max(0, int(os.getenv("IBO_MAIL_CAP_TOTAL", str(default)) or default))
    except ValueError:
        return default


def cap_critical_per_system() -> int:
    """
    CRITICAL / SYSTEM DOWN / VPN / credential mails do not count against the
    everyday caps -- a first CRITICAL must reach someone even after a noisy
    day of warnings. This is only a flood guard (IBO_MAIL_CAP_CRITICAL, 200
    per system in 24 h).
    """
    try:
        return max(0, int(os.getenv("IBO_MAIL_CAP_CRITICAL", "200") or 200))
    except ValueError:
        return 200


_KIND_LABELS = {"system_down": "SYSTEM DOWN", "vpn_down": "VPN NOT CONNECTED",
                "not_reachable": "NOT REACHABLE", "credentials": "CREDENTIALS (logon refused)",
                "collector_down": "Collector / tunnel", "recovered": "RECOVERED",
                "critical": "Critical alert", "tcode_failure": "T-code capture failed",
                "sweep_failure": "Pipeline failed", "report_held": "REPORT HELD", "report_not_sent": "REPORT NOT SENT",
                "report_sent": "Report sent", "monitor_down": "Monitor down",
                "ocr_parser": "Parser check", "job_owner": "Job owner mail",
                "dump_owner": "Dump owner mail", "rca": "Performance RCA",
                "report": "Monitoring report", "final_report": "Monitoring report",
                "diagnostic": "Diagnostic alert (RCA)"}


def _critical(kind: str, severity: str) -> bool:
    # Same set as notifications/mailer.CRITICAL_KINDS (SYSTEM DOWN, CRITICAL,
    # monitor down, credentials and VPN alerts).
    return kind in ("system_down", "critical", "monitor_down", "credentials", "vpn_down") \
        or str(severity).upper() == "CRITICAL"


def history(system: str, kind: str, key, severity: str, ok: bool, note: str,
            recipients="", decision: str = "") -> None:
    """One row on the Alert history page (alert_sends) for a gate decision. Never raises."""
    try:
        from core import alert_engine
        rec = recipients if isinstance(recipients, str) else ", ".join(recipients or [])
        alert_engine.record(system, kind, "", _KIND_LABELS.get(kind, kind),
                            (str(severity or "") or "INFO").upper(), str(key or "")[:120], "",
                            decision or ("sent" if ok else "suppressed"), "email", rec, ok, note)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"alert history row not written: {exc}")


def _hold(now, system, kind, key, reason, severity="", mirror=True) -> None:
    try:
        with _lock, _connect() as conn:
            wrote = _record_hold(conn, now, system, kind, key, reason)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ledger hold not recorded: {exc}")
        wrote = True
    if wrote and mirror:
        history(system, kind, key, severity, False, reason, decision=reason.split(":", 1)[0])


def should_send(system: str, kind: str, key=None, min_interval_s: float = 0,
                daily_cap: int | None = None, severity: str = "",
                mirror: bool = True, check_silences: bool = True, closing: bool = False) -> tuple[bool, str]:
    """
    (True, "") when the alert may go out; (False, reason) otherwise.
    A suppressed decision is written to the ledger (and the Alert history
    page unless mirror=False -- the per-value engine writes its own rows).
    """
    key = _norm_key(key)
    cap = _default_cap(kind) if daily_cap is None else daily_cap
    now = datetime.now()
    if kind in disabled_kinds():
        reason = (f"suppressed-policy: {kind} mails are switched off "
                  f"(IBO_DISABLED_MAIL_KINDS)")
        _hold(now, system, kind, key, reason, severity, mirror)
        return False, reason
    # Maintenance windows, snoozes and acknowledgements (core/alert_control.py)
    # are checked first, for every alert, so no sender can forget them.
    held = _gate(system, kind, key) if check_silences else None
    if held:
        try:
            with _lock, _connect() as conn:
                wrote = _record_hold(conn, now, system, kind, key, held)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"ledger hold not recorded: {exc}")
            wrote = False
        if wrote and mirror:
            history(system, kind, key, severity, False, f"suppressed-silenced: {held}",
                    decision="suppressed-silenced")
        return False, held
    critical = _critical(kind, severity)
    outage = _mail_outage(system)
    if outage and not critical:
        reason = f"suppressed-mail-outage: {outage}"
        _hold(now, system, kind, key, reason, severity, mirror)
        return False, reason
    if outage and critical:
        # The mail still goes (secondary account, then the main account
        # anyway), but it may well fail: a SYSTEM DOWN must reach someone.
        _whatsapp_copy(system, kind, key, _KIND_LABELS.get(kind, kind),
                       f"mail account suspended ({outage})")
    with _lock, _connect() as conn:
        # Check-and-reserve in ONE write transaction. Two alerts for the same
        # thing raised at the same moment (two systems behind one VPN, or two
        # processes) used to both pass the check before either recorded its
        # send, and both mailed. Now the first reserves the slot; the second
        # sees it and is held. BEGIN IMMEDIATE also serialises processes.
        conn.execute("BEGIN IMMEDIATE")
        try:
            allowed, reason, wrote = _decide(conn, now, system, kind, key, cap, min_interval_s, critical, closing)
            if allowed:
                _record(conn, now, system, kind, key, "", "", "", 0, _RESERVED)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    if not allowed:
        if wrote and mirror:
            history(system, kind, key, severity, False, reason, decision=reason.split(":", 1)[0])
        if critical and reason.startswith("suppressed-cap"):
            # Mail is capped; a SYSTEM DOWN must still reach someone.
            _whatsapp_copy(system, kind, key, _KIND_LABELS.get(kind, kind), reason)
    return allowed, reason


def _mail_outage(system: str) -> str:
    try:
        from notifications import mailer
        smtp = None
        try:
            from core.config_loader import get_smtp_config
            name = system.split(":", 1)[1] if system.startswith("network:") else system
            smtp = get_smtp_config(mailer._cfg_for(name), require_recipients=False)
        except Exception:  # noqa: BLE001
            smtp = None
        return mailer.mail_suspended(smtp)
    except Exception:  # noqa: BLE001
        return ""


def _whatsapp_copy(system: str, kind: str, key, subject: str, reason: str) -> None:
    try:
        from notifications import whatsapp
        if whatsapp.wants_alert(kind) and _whatsapp_due(system, kind, key):
            name = system.split(":", 1)[1].split(":")[0] if system.startswith("network:") else system
            import threading as _th
            _th.Thread(target=whatsapp.send_alert, args=(system, kind, subject or kind,
                                                         f"{name}: {reason or ''}".strip(": ")),
                       daemon=True, name=f"wa-{kind}").start()
    except Exception as exc:  # noqa: BLE001
        log.debug(f"whatsapp copy failed: {exc}")


_RESERVED = "reserved (sending)"


def _gate(system: str, kind: str, key) -> str | None:
    try:
        from core.alert_control import gate
        return gate(system, kind, key)
    except Exception as exc:  # noqa: BLE001 -- the gate fails open
        log.debug(f"alert gate unavailable: {exc}")
        return None


def _after_send(system: str, kind: str, key: str, severity: str, subject: str,
                recipients: str, delivered: bool, reason: str) -> None:
    """Timeline line for every mail that went out (or failed), and tickets for red ones."""
    try:
        from core import timeline
        sev = {"CRITICAL": "critical", "ALERT": "critical", "WARNING": "warning",
               "INFO": "ok"}.get(str(severity).upper(), "info")
        if not delivered:
            sev = "warning"
        target = system
        if system.startswith("network:"):
            from core.alert_control import _network_members
            members = _network_members(system) or [system]
        else:
            members = [target]
        for m in members:
            timeline.add(m, "alert", (subject or f"{kind} alert") if delivered
                         else f"Alert NOT delivered: {subject or kind}",
                         (f"to {recipients}" if delivered else reason), severity=sev,
                         ref=f"alert:{kind}:{key}")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"timeline for alert failed: {exc}")
    if delivered:
        try:
            from core import tickets
            tickets.on_alert_sent(system, kind, key, severity, subject)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"ticket hook failed: {exc}")
    # WhatsApp copy (notifications/whatsapp.send_alert) -- also when the mail
    # FAILED, so a dead SMTP server does not silence a SYSTEM DOWN.
    _whatsapp_copy(system, kind, key, subject, reason)


def _whatsapp_due(system: str, kind: str, key) -> bool:
    """At most one WhatsApp per alert (system, kind, key) per
    IBO_WHATSAPP_MIN_MINUTES (default 30) -- whether the mail went out or
    not. A failed mail is retried by its sender every minute; WhatsApp must
    not follow it."""
    try:
        from core import attempts
        try:
            minutes = float(os.getenv("IBO_WHATSAPP_MIN_MINUTES", "30") or 30)
        except ValueError:
            minutes = 30.0
        scope = f"wa:{system}:{kind}:{key}"
        if not attempts.allowed(scope, repeat_after_s=minutes * 60):
            return False
        attempts.done(scope, True)
        return True
    except Exception:
        return False


def _decide(conn, now, system, kind, key, cap, min_interval_s, critical=False,
            closing=False) -> tuple[bool, str, bool]:
    """(allowed, reason, hold row written)."""
    if not caps_on():
        cap = 0                      # no daily limit per kind either (owner's rule, 28.10.2026)
    if min_interval_s > 0:
        row = conn.execute(
            "SELECT sent_at FROM alerts WHERE system=? AND kind=? AND key=? AND suppressed=0 "
            "ORDER BY sent_at DESC LIMIT 1", (system, kind, key)).fetchone()
        if row:
            last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            if now - last < timedelta(seconds=min_interval_s):
                reason = (f"suppressed-dedup: same {kind} alert sent at {row[0]}, repeat held for "
                          f"{int(min_interval_s // 60)} min")
                return False, reason, _record_hold(conn, now, system, kind, key, reason)
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    sent_today = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE system=? AND kind=? AND suppressed=0 AND sent_at>=?",
        (system, kind, day_start)).fetchone()[0]
    if cap and sent_today >= cap:
        last = conn.execute("SELECT MAX(sent_at) FROM alerts WHERE system=? AND kind=? AND suppressed=0",
                            (system, kind)).fetchone()[0]
        hourly_ok = critical and (not last or now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                                  >= timedelta(minutes=60))
        if not hourly_ok:
            # A SYSTEM DOWN that lasts all night: after `cap` reminders it does
            # not go silent, it slows down to one an hour.
            reason = (f"suppressed-cap: {cap} {_KIND_LABELS.get(kind, kind)} mails today for {system}; "
                      + ("from now on one an hour while it lasts" if critical else
                         "no more of this kind today (IBO_ALERT_DAILY_CAP)"))
            return False, reason, _record_hold(conn, now, system, kind, key, reason)
    # Rolling 24 h (Gmail's limit is rolling too), every kind together.
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    if not caps_on():
        return True, "", False       # no mail limits for any system
    if critical or closing:
        # Not held by the everyday caps (below); only a flood guard. A
        # "back to normal" (closing) is the mail that ends someone's work on
        # an alert they already got -- holding it leaves them chasing a
        # problem that is gone.
        flood = cap_critical_per_system()
        if flood:
            n = conn.execute("SELECT COUNT(*) FROM alerts WHERE system=? AND suppressed=0 AND sent_at>=?",
                             (system, since)).fetchone()[0]
            if n >= flood:
                reason = (f"suppressed-cap: {flood} mails for {system} in 24 h, even CRITICAL ones "
                          f"(IBO_MAIL_CAP_CRITICAL)")
                return False, reason, _record_hold(conn, now, system, kind, key, reason)
        return True, "", False
    per_sys = cap_per_system(system)
    if per_sys:
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE system=? AND suppressed=0 AND sent_at>=?",
                         (system, since)).fetchone()[0]
        if n >= per_sys:
            reason = (f"suppressed-cap: {per_sys} mails for {system} in 24 h "
                      f"(IBO_MAIL_CAP_PER_SYSTEM)")
            return False, reason, _record_hold(conn, now, system, kind, key, reason)
    total = cap_total()
    if total:
        n = conn.execute("SELECT COUNT(*) FROM alerts WHERE suppressed=0 AND sent_at>=?",
                         (since,)).fetchone()[0]
        if n >= total:
            reason = f"suppressed-cap: {total} mails in 24 h for all systems (IBO_MAIL_CAP_TOTAL)"
            return False, reason, _record_hold(conn, now, system, kind, key, reason)
    return True, "", False


def _record_hold(conn, when, system, kind, key, reason, every_s: int = 600) -> bool:
    """
    Note a held repeat -- but at most once per `every_s` per (system, kind,
    key). Without this, a check that runs every 20 s wrote a "held" row
    every 20 s, and /api/alerts was nothing but holds. True when written.
    """
    row = conn.execute(
        "SELECT sent_at FROM alerts WHERE system=? AND kind=? AND key=? AND suppressed=1 "
        "ORDER BY id DESC LIMIT 1", (system, kind, key)).fetchone()
    if row:
        try:
            if when - datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") < timedelta(seconds=every_s):
                return False
        except ValueError:
            pass
    _record(conn, when, system, kind, key, "", "", "", 1, reason)
    return True


def _record(conn, when, system, kind, key, severity, subject, recipients, suppressed, reason):
    conn.execute(
        "INSERT INTO alerts(sent_at, system, kind, key, severity, subject, recipients, suppressed, reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (when.strftime("%Y-%m-%d %H:%M:%S"), system, kind, key, severity, subject,
         recipients if isinstance(recipients, str) else ",".join(recipients or []),
         int(suppressed), reason))


def record(system: str, kind: str, key=None, severity: str = "", subject: str = "",
           recipients=None, delivered: bool = True, reason: str = "",
           mirror: bool = True, hooks: bool = True) -> None:
    """Write one sent (or failed) alert. Never raises.
    mirror: also a row on the Alert history page; hooks: timeline, ticket,
    WhatsApp copy (the per-value engine does its own and passes False)."""
    try:
        key = _norm_key(key)
        rec = recipients if isinstance(recipients, str) else ",".join(recipients or [])
        with _lock, _connect() as conn:
            # Fill in the slot should_send() reserved, if there is one.
            row = conn.execute(
                "SELECT id FROM alerts WHERE system=? AND kind=? AND key=? AND reason=? "
                "ORDER BY id DESC LIMIT 1", (system, kind, key, _RESERVED)).fetchone()
            if row:
                conn.execute(
                    "UPDATE alerts SET severity=?, subject=?, recipients=?, suppressed=?, reason=? "
                    "WHERE id=?", (severity, subject, rec, 0 if delivered else 1,
                                   reason or ("" if delivered else "send failed"), row[0]))
            else:
                _record(conn, datetime.now(), system, kind, key, severity, subject,
                        recipients or [], 0 if delivered else 1,
                        reason or ("" if delivered else "send failed"))
    except Exception as exc:
        log.warning(f"alert ledger write failed: {exc}")
        return
    if mirror:
        history(system, kind, key, severity, delivered,
                (subject if delivered else f"send failed: {reason or 'see logs/email.log'}"),
                recipients=rec, decision="sent" if delivered else "send failed")
    if hooks:
        _after_send(system, kind, key, severity, subject, rec, delivered, reason)


def recent(limit: int = 100, system: str | None = None) -> list[dict]:
    try:
        with _lock, _connect() as conn:
            conn.row_factory = sqlite3.Row
            if system:
                rows = conn.execute("SELECT * FROM alerts WHERE system=? ORDER BY id DESC LIMIT ?",
                                    (system, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        log.warning(f"alert ledger read failed: {exc}")
        return []


def today_summary() -> dict:
    """{system: {kind: {"sent": n, "suppressed": m}}} for today."""
    out: dict = {}
    try:
        with _lock, _connect() as conn:
            day_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
            for system, kind, suppressed, n in conn.execute(
                    "SELECT system, kind, suppressed, COUNT(*) FROM alerts WHERE sent_at>=? "
                    "GROUP BY system, kind, suppressed", (day_start,)):
                bucket = out.setdefault(system, {}).setdefault(kind, {"sent": 0, "suppressed": 0})
                bucket["suppressed" if suppressed else "sent"] += n
    except Exception as exc:
        log.warning(f"alert ledger summary failed: {exc}")
    return out


def prune(days: int = 90) -> int:
    try:
        with _lock, _connect() as conn:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cur = conn.execute("DELETE FROM alerts WHERE sent_at < ?", (cutoff,))
            return cur.rowcount
    except Exception as exc:
        log.warning(f"alert ledger prune failed: {exc}")
        return 0

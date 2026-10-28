"""
One timeline per system: everything that happened to it, in order.

"What happened to PS4 at 3 am?" used to mean reading three logs, the alert
ledger and the mailbox. Every part of the platform that changes something
or learns something now writes one line here:

    alert        a mail went out (DOWN, VPN, critical, credentials, ...)
    state        reachability changed (VPN NOT CONNECTED -> SYSTEM DOWN -> OK)
    sweep        a sweep completed, failed or was skipped (and why)
    maintenance  a window was set, ended or cancelled
    silence      snooze / ignore-until-fixed / acknowledge, and clears
    park         sweeps parked or resumed
    ticket       ticket opened / closed
    action       a one-click SAP action requested, approved, done, failed
    runbook      a runbook step ticked
    kb           a fix saved to the knowledge base
    incident     incident acknowledged / resolved on the Incidents page
    baseline     a value unusual for this time of day
    capacity     a resource forecast to run out

`add()` never raises: a timeline write must never break the thing it
describes. Rows are kept for IBO_TIMELINE_DAYS (default 180).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

CATEGORIES = ("alert", "state", "sweep", "maintenance", "silence", "park", "ticket",
              "action", "runbook", "kb", "incident", "baseline", "capacity", "vpn", "config",
              "error", "change", "escalation", "correlation", "check", "synthetic", "failover")
SEVERITIES = ("info", "ok", "warning", "critical")


def actor_name(default: str = "system") -> str:
    """The logged-in user for this request, or `default` outside a request."""
    try:
        from core.auth import current_user
        u = current_user.get()
        if u and u.get("username"):
            return str(u["username"])
    except Exception:
        pass
    return default


def add(system: str, category: str, title: str, detail: str = "", actor: str = "",
        severity: str = "info", ref: str = "", at: datetime | None = None) -> None:
    try:
        from core import ops_db
        ops_db.execute(
            "INSERT INTO timeline(at, system, category, title, detail, actor, severity, ref) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ops_db.fmt(at or datetime.now()), str(system or "?")[:120], category[:24],
             str(title)[:300], str(detail or "")[:4000], str(actor or "")[:120],
             severity if severity in SEVERITIES else "info", str(ref or "")[:200]))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"timeline write failed: {exc}")


def query(system: str | None = None, days: float = 7, categories: list[str] | None = None,
          since: str | None = None, until: str | None = None, limit: int = 1000,
          systems: list[str] | None = None) -> list[dict]:
    """Newest first. `systems` limits to a customer scope (None = all)."""
    from core import ops_db
    sql = "SELECT * FROM timeline WHERE 1=1"
    args: list = []
    if system:
        sql += " AND system=?"
        args.append(system)
    elif systems is not None:
        if not systems:
            return []
        sql += " AND system IN (%s)" % ",".join("?" * len(systems))
        args += list(systems)
    start = ops_db.parse(since) if since else datetime.now() - timedelta(days=max(0.01, days))
    sql += " AND at>=?"
    args.append(ops_db.fmt(start))
    if until:
        sql += " AND at<=?"
        args.append(ops_db.fmt(ops_db.parse(until)) or until)
    if categories:
        sql += " AND category IN (%s)" % ",".join("?" * len(categories))
        args += list(categories)
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    args.append(int(max(1, min(limit, 5000))))
    try:
        return ops_db.rows(sql, args)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"timeline read failed: {exc}")
        return []


def prune(days: int | None = None) -> int:
    from core import ops_db
    try:
        keep = int(days or os.getenv("IBO_TIMELINE_DAYS", "180") or 180)
    except ValueError:
        keep = 180
    cutoff = ops_db.fmt(datetime.now() - timedelta(days=keep))
    try:
        return ops_db.execute("DELETE FROM timeline WHERE at<?", (cutoff,))
    except Exception:
        return 0

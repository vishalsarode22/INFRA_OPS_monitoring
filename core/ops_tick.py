"""
The once-a-minute housekeeping for the operations features. Runs in exactly
one process: the jobs service in a split deployment, the dashboard itself in
the single-process one (IBO_ROLE=all, which is how both VMs run today).

    every minute   heartbeat to the other dashboard; silent-peer check;
                   maintenance windows that just ended -> timeline
    every 5 min    ticket status from Jira / ServiceNow; ticket retries;
                   expire unapproved SAP actions
    once a day     capacity forecasts (IBO_FORECAST_HOUR, default 08);
                   timeline pruning
Never raises.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

_last: dict[str, float] = {}


def _meta_get(key: str) -> str | None:
    from core import store
    row = store.conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(key: str, value: str) -> None:
    from core import store
    store.conn().execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))


def _every(name: str, seconds: float) -> bool:
    now = time.monotonic()
    if now - _last.get(name, -1e9) >= seconds:
        _last[name] = now
        return True
    return False


def _step(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"ops tick: {name} failed: {type(exc).__name__}: {exc}")


def _maintenance_ended() -> None:
    from core import alert_control, ops_db, timeline
    since = _meta_get("ops_maint_checked") or ops_db.now()
    for w in alert_control.windows_ended_since(since):
        if w.get("cancelled_at"):
            continue          # cancel_window() already wrote the line
        for s in alert_control._expand(w["system"]):
            timeline.add(s, "maintenance", "Maintenance window ended -- alerts and sweeps active again",
                         w.get("reason", ""), severity="ok", ref=f"maintenance:{w['id']}")
    _meta_set("ops_maint_checked", ops_db.now())


def _daily() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        hour = int(os.getenv("IBO_FORECAST_HOUR", "8") or 8)
    except ValueError:
        hour = 8
    if datetime.now().hour < hour or _meta_get("ops_daily") == today:
        return
    _meta_set("ops_daily", today)
    from core import capacity, timeline
    _step("capacity", capacity.check_all)
    _step("timeline prune", timeline.prune)
    from core import errors
    _step("error prune", errors.prune)
    from core import attempts
    _step("send attempts prune", attempts.prune)


def run() -> None:
    try:
        from core import live_settings
        live_settings.sync()          # a change made by another process of this dashboard
    except Exception:  # noqa: BLE001
        pass
    from core import peer_watch, sap_actions, tickets
    if _every("peer_push", 55):
        _step("peer heartbeat", peer_watch.push_heartbeats)
    if _every("peer_check", 55):
        _step("peer check", peer_watch.check_peers)
    if _every("maint", 55):
        _step("maintenance", _maintenance_ended)
    if _every("tickets", 295):
        _step("ticket sync", tickets.sync)
        _step("ticket retry", tickets.retry_failed_creates)
        _step("action expiry", sap_actions.expire_old)
    if _every("escalation", 55):
        from core import escalation_time
        _step("escalation", escalation_time.run)
    if _every("synthetic", 55):
        from core import synthetic
        _step("synthetic", synthetic.maybe_start)
    if _every("correlation", 290):
        from core import correlation_x
        _step("cross-system correlation", correlation_x.run)
    if _every("check_alerts", 55):
        from core import check_alerts
        _step("check alerts", check_alerts.maybe_start)
    if _every("custom_alerts", 55):
        from core import custom_alerts
        _step("custom alerts", custom_alerts.maybe_start)
    if _every("digest", 600):
        from core import digest
        _step("weekly digest", digest.send)
    if _every("daily", 600):
        _step("daily", _daily)

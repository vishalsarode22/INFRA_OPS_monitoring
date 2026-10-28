"""
A scheduled monitoring run that did not happen is said, not just logged.

On 13 Oct the daily sheets of several systems did not come and nothing said
why: automatic monitoring had been paused by the Stop button the evening
before, and a passed slot only went to the log and the timeline. Now the
Basis team (internal addresses only, core/report_gate._basis) gets one mail
per system and slot when a saved schedule on the Scheduler page:

    * falls due while automatic monitoring is paused or snoozed;
    * is skipped because the system's sweeps are parked (e.g. the SAP logon
      was refused) -- a parked flag is easy to forget;
    * could not start within the hour (another sweep ran too long).

Maintenance windows, standby and a VPN switched off on purpose are chosen
by a person and are not mailed. IBO_SCHEDULE_NOTICE=0 switches this off.
"""
from __future__ import annotations

import os
from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__)

_KEY = "schedule_notice:"


def enabled() -> bool:
    return os.getenv("IBO_SCHEDULE_NOTICE", "1").strip().lower() not in ("0", "false", "no", "off")


_sent_here: set = set()


def _once(system: str, slot: datetime, kind: str = "slot") -> bool:
    """
    True the first time for this system, kind and slot. Kept in the database
    (a restart does not repeat it) and in memory (a database that cannot be
    written does not turn into a mail every 20 seconds).
    """
    from core import ops_db
    key = f"{_KEY}{kind}:{system}"
    stamp = slot.strftime("%Y-%m-%d %H:%M")
    if (key, stamp) in _sent_here:
        return False
    _sent_here.add((key, stamp))
    try:
        row = ops_db.one("SELECT value FROM meta WHERE key=?", (key,))
        if row and str(row["value"]) == stamp:
            return False
        ops_db.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                       "value=excluded.value", (key, stamp))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"schedule notice memory: {exc}")
    return True


def not_run(system_cfg: dict, slot: datetime, reason: str, advice: str, now: datetime | None = None,
            once_per_day: bool = False, kind: str = "slot") -> bool:
    """Tell Basis that the scheduled run of `slot` did not (or will not) happen. Once per slot (or day)."""
    if not enabled() or not system_cfg:
        return False
    name = str(system_cfg.get("name") or "")
    if not name or not _once(name, slot.replace(hour=0, minute=0) if once_per_day else slot, kind):
        return False
    now = now or datetime.now()
    from html import escape as e
    when = f"{slot:%d.%m.%Y}" if (slot.hour, slot.minute) == (0, 0) else f"{slot:%d.%m.%Y %H:%M}"
    html = ("<html><body style='font-family:Segoe UI,Arial,sans-serif;color:#0f172a'>"
            f"<h2 style='color:#b45309;margin:0 0 10px'>Scheduled monitoring did not run: {e(name)}</h2>"
            "<table style='border-collapse:collapse;font-size:14px'>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>Scheduled for</td><td><b>{when}</b></td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>Why</td><td><b>{e(reason)}</b></td></tr>"
            f"<tr><td style='padding:4px 12px 4px 0;color:#64748b'>What to do</td><td>{e(advice)}</td></tr>"
            "</table><p>No monitoring sheet or PDF went to the client for this run.</p>"
            f"<p style='color:#64748b'>Checked {now:%d.%m.%Y %H:%M}. Internal mail for the Basis team only.</p>"
            "</body></html>")
    subject = (f"[NOT RUN] InfraBeatOps -- {name}: scheduled monitoring "
               + (f"of {slot:%H:%M} " if (slot.hour, slot.minute) != (0, 0) else "today ") + "did not run")
    try:
        from core import report_gate
        ok = report_gate._send_internal(system_cfg, subject, html, "schedule_not_run",
                                        f"{slot:%Y%m%d%H%M}", "WARNING")
    except Exception as exc:  # noqa: BLE001
        log.error(f"{name}: schedule notice mail failed: {exc}")
        ok = False
    log.warning(f"{name}: scheduled monitoring of {slot:%Y-%m-%d %H:%M} did not run -- {reason}"
                f" (Basis {'told' if ok else 'NOT told'})")
    try:
        from core import timeline
        timeline.add(name, "sweep", "Scheduled monitoring did not run", f"{reason}. {advice}",
                     severity="warning")
    except Exception:  # noqa: BLE001
        pass
    return ok


def due_while_paused(system_names: list[str], now: datetime | None = None) -> list[tuple[str, datetime]]:
    """
    (system, slot) for each saved schedule whose slot has passed while the
    scheduler was not running it. Daily schedules: today's slot once it is
    due. Interval schedules: once a day while they are due.
    """
    from core import schedules as sch
    now = now or datetime.now()
    stored = sch.load_schedules()
    out = []
    for name in system_names:
        cfg = stored.get(name)
        if not cfg or not cfg.get("enabled"):
            continue
        try:
            c = sch._normalise(cfg)
            last = sch.last_run(name)
            if c["mode"] == "daily":
                hh, mm = (int(x) for x in c["at"].split(":"))
                slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if sch.WEEKDAYS[slot.weekday()] not in c["days"] or now < slot:
                    continue
                if last is not None and last >= slot:
                    continue
                out.append((name, slot))
            elif sch.is_due(c, last, now) and not sch.stale_interval(c, last, now):
                out.append((name, now.replace(hour=0, minute=0, second=0, microsecond=0)))
        except Exception as exc:  # noqa: BLE001
            log.debug(f"{name}: schedule check failed: {exc}")
    return out

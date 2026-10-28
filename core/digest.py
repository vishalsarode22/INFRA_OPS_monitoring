"""
Weekly digest mail: Monday morning, one mail to Basis and the manager.

    per system   availability %, outages, minutes down, alerts sent (top kinds),
                 open problems now, capacity running out within 60 days,
                 values unusual for their time, changes recorded, tickets
    platform     noisiest alerts (to retune), new error kinds, fixes added
                 to the knowledge base

IBO_DIGEST_DAY (mon..sun, default mon) and IBO_DIGEST_HOUR (default 8);
IBO_DIGEST=0 turns it off. Sent once per week (the ledger key is the ISO
week), from whichever process runs the ops tick.
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def due(now: datetime | None = None) -> bool:
    if os.getenv("IBO_DIGEST", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    now = now or datetime.now()
    day = (os.getenv("IBO_DIGEST_DAY") or "mon").strip().lower()[:3]
    try:
        hour = int(os.getenv("IBO_DIGEST_HOUR", "8") or 8)
    except ValueError:
        hour = 8
    return day in _DAYS and now.weekday() == _DAYS.index(day) and now.hour >= hour


def build(days: int = 7) -> dict:
    from core import capacity, insights, ops_db
    from core.config_loader import get_all_systems
    names = [c.get("name") for c in get_all_systems() if c.get("name")]
    since = ops_db.fmt(datetime.now() - timedelta(days=days))
    avail = {a["system"]: a for a in insights.availability(days, names)}
    try:
        from core import standby
        from core.reachability import current_failures
        open_now = standby.only_active(current_failures())
    except Exception:
        open_now = {}
    systems = []
    for n in names:
        sent = ops_db.rows("SELECT kind, COUNT(*) AS c FROM alerts WHERE system=? AND suppressed=0 AND sent_at>=? "
                           "GROUP BY kind ORDER BY c DESC", (n, since))
        changes = ops_db.one("SELECT COUNT(*) AS c FROM timeline WHERE system=? AND category='change' AND at>=?",
                             (n, since))["c"]
        tickets = ops_db.one("SELECT COUNT(*) AS c FROM tickets WHERE system=? AND created_at>=?", (n, since))["c"]
        try:
            caps = [f for f in capacity.forecasts(n) if f.get("state") == "full" or
                    (f.get("state") == "growing" and f.get("days_left", 1e9) <= 60)]
        except Exception:
            caps = []
        try:
            from core.learned_baseline import flags
            unusual = flags(n).get("unusual") or []
        except Exception:
            unusual = []
        systems.append({"system": n, "availability": avail.get(n, {}), "alerts": sent, "changes": changes,
                        "tickets": tickets, "capacity": caps, "unusual": unusual, "open": open_now.get(n)})
    noisy = [x for x in insights.noise(days, names) if x["tip"]][:5]
    errs = ops_db.rows("SELECT etype, message, count FROM errors WHERE first_seen>=? ORDER BY count DESC LIMIT 5",
                       (since,)) if _has_errors_table() else []
    kb = ops_db.rows("SELECT title, system, created_by FROM kb WHERE created_at>=? AND retired=0", (since,))
    return {"days": days, "systems": systems, "noisy": noisy, "errors": errs, "kb": kb,
            "from": since[:10], "to": datetime.now().strftime("%Y-%m-%d")}


def _has_errors_table() -> bool:
    try:
        from core import errors
        errors._db()
        return True
    except Exception:
        return False


def to_html(d: dict) -> str:
    e = lambda x: html.escape(str(x if x is not None else ""))  # noqa: E731
    rows = []
    for s in d["systems"]:
        a = s["availability"] or {}
        top = ", ".join(f"{e(x['kind'])} {x['c']}" for x in s["alerts"][:3]) or "none"
        notes = []
        if s["open"]:
            notes.append(f"<b style='color:#b91c1c'>open now: {e(s['open'].get('state'))}</b>")
        for c in s["capacity"][:2]:
            notes.append(f"{e(c['metric'])}: {e(c.get('text', ''))[:90]}")
        if s["unusual"]:
            notes.append("unusual: " + ", ".join(e(u["metric"]) for u in s["unusual"][:3]))
        if s["changes"]:
            notes.append(f"{s['changes']} change(s)")
        if s["tickets"]:
            notes.append(f"{s['tickets']} ticket(s)")
        pct = a.get("availability_pct")
        colour = "#15803d" if pct is None or pct >= 99.5 else "#b45309" if pct >= 98 else "#b91c1c"
        rows.append(f"<tr><td><b>{e(s['system'])}</b></td><td style='color:{colour}'>"
                    f"{'-' if pct is None else f'{pct}%'}</td><td>{a.get('outages', 0)} / {a.get('down_minutes', 0)} min</td>"
                    f"<td>{top}</td><td>{'<br>'.join(notes) or '-'}</td></tr>")
    noisy = "".join(f"<li>{e(n['system'])} {e(n['kind'])}: {n['sent']} sent, snoozed {n['snoozed']}, ignored "
                    f"{n['ignored']} -- {e(n['tip'])}</li>" for n in d["noisy"]) or "<li>nothing stands out</li>"
    errs = "".join(f"<li>{e(x['etype'])}: {e(x['message'])[:120]} ({x['count']}x)</li>" for x in d["errors"]) \
        or "<li>none</li>"
    kb = "".join(f"<li>{e(k['title'])} ({e(k['system'] or 'any')}, {e(k['created_by'])})</li>" for k in d["kb"]) \
        or "<li>none</li>"
    return (f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#0f172a'>"
            f"<h2 style='font-size:18px'>InfraBeatOps weekly digest -- {e(d['from'])} to {e(d['to'])}</h2>"
            f"<table cellpadding='6' style='border-collapse:collapse;width:100%' border='1' bordercolor='#e2e8f0'>"
            f"<tr style='background:#f1f5f9'><th align=left>System</th><th align=left>Availability</th>"
            f"<th align=left>Outages / down</th><th align=left>Alerts sent</th><th align=left>Notes</th></tr>"
            f"{''.join(rows)}</table>"
            f"<h3>Alerts to retune</h3><ul>{noisy}</ul><h3>New error kinds on the platform</h3><ul>{errs}</ul>"
            f"<h3>Fixes added to the knowledge base</h3><ul>{kb}</ul>"
            f"<p style='color:#64748b'>Availability counts SYSTEM DOWN time only (not VPN or monitoring problems).</p></div>")


def send(force: bool = False) -> bool:
    from core import recipients
    from core.config_loader import get_smtp_config
    from notifications.mailer import send_html
    now = datetime.now()
    if not force and not due(now):
        return False
    to, cc = recipients.basis_and_manager(None)
    if not to:
        return False
    week = now.strftime("%G-W%V")
    if not force:
        from core import ops_db
        if ops_db.one("SELECT 1 AS x FROM alerts WHERE system='IBOPS' AND kind='digest' AND key=? AND suppressed=0",
                      (week,)):
            return False        # already sent this week; skip the build
    d = build(7)
    return bool(send_html(subject=f"InfraBeatOps weekly digest {d['from']} - {d['to']}", html=to_html(d), to=to,
                          cc=cc, smtp_config=get_smtp_config(None, require_recipients=False), system="IBOPS",
                          kind="digest", key=week + ("-manual" + now.strftime("%H%M%S") if force else ""),
                          min_interval_s=6 * 86400 if not force else 0, severity="INFO"))

"""
Ticket creation: a red alert opens a ticket in the tool used for client
tickets, with the screenshot and the AI summary attached; closing the
ticket clears the alert.

CONFIGURATION (.env)
    IBO_TICKETS=off | email | webhook | jira | servicenow      (default off)
    IBO_TICKET_KINDS=system_down,critical    alert kinds that open a ticket
    IBO_TICKET_CRITICAL_TIER=red             critical alerts: only RED (repeat)
                                             breaches open one; "any" for yellow too

  email       IBO_TICKET_EMAIL=helpdesk@...   mail-to-ticket address of the tool
              (works with almost every helpdesk). The ticket number is not
              known to us: close it on the Operations page when the
              helpdesk ticket is closed.
  webhook     IBO_TICKET_WEBHOOK_URL, IBO_TICKET_WEBHOOK_SECRET
              We POST {"event": "open"|"note", ...}; an answer with {"id","url"}
              is stored. The tool closes it by calling (no login needed)
              POST /access/ticket/<our id>/close  with header X-IBO-Ticket-Secret
              and an optional JSON body {"closed_by": "...", "resolution": "..."}.
  jira        IBO_JIRA_URL, IBO_JIRA_USER, IBO_JIRA_TOKEN, IBO_JIRA_PROJECT,
              IBO_JIRA_ISSUETYPE (default Task)      REST API v2
  servicenow  IBO_SNOW_URL, IBO_SNOW_USER, IBO_SNOW_PASSWORD,
              IBO_SNOW_ASSIGNMENT_GROUP (optional)   Table API, incident

ONE TICKET PER PROBLEM
At most one open ticket per (system, alert kind). Repeats of the same
alert while it is open do not open more; a recovery adds a note.

CLOSING
jira / servicenow are polled every 5 minutes (ops tick). When the ticket is
closed (Jira status category "done", ServiceNow state Resolved/Closed):
  * acknowledgements for the system are cleared and parked sweeps resume,
  * a critical alert's escalation tier is reset,
  * ServiceNow close notes are saved to the knowledge base,
  * the timeline records who closed it.
If the problem is still there, the next alert opens a new ticket -- a
ticket closed while the system is still down is reopened, by design.
"""
from __future__ import annotations

import html as _html
import json
import mimetypes
import os
import threading
from datetime import datetime

from core import ops_db, timeline
from utils.logger import get_logger

log = get_logger(__name__)

PROVIDERS = ("email", "webhook", "jira", "servicenow")


def provider() -> str:
    p = (os.getenv("IBO_TICKETS") or "off").strip().lower()
    return p if p in PROVIDERS else "off"


def _kinds() -> set[str]:
    raw = os.getenv("IBO_TICKET_KINDS", "system_down,critical")
    return {k.strip() for k in raw.split(",") if k.strip()}


def wants_ticket(system: str, kind: str, key: str, severity: str) -> bool:
    if provider() == "off" or kind not in _kinds() or system.startswith("network:"):
        return False
    if kind == "critical":
        tier = os.getenv("IBO_TICKET_CRITICAL_TIER", "red").strip().lower()
        if tier != "any" and "RED" not in str(key or "").split(","):
            return False
    return True


def open_ticket(system: str, kind: str) -> dict | None:
    return ops_db.one("SELECT * FROM tickets WHERE system=? AND kind=? AND status IN ('opening','open') "
                      "ORDER BY id DESC LIMIT 1", (system, kind))


def on_alert_sent(system: str, kind: str, key, severity: str, subject: str) -> None:
    """Called by the alert ledger after every delivered alert. Never raises."""
    try:
        key = str(key or "")
        if not wants_ticket(system, kind, key, severity):
            return
        if open_ticket(system, kind):
            return
        tid = ops_db.execute(
            "INSERT INTO tickets(system, kind, key, provider, status, subject, created_at) "
            "VALUES (?,?,?,?,?,?,?)", (system, kind, key, provider(), "opening", subject[:300], ops_db.now()))
        _dispatch("ticket_create", {"ticket_id": tid})
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{system}: ticket not requested: {exc}")


def _dispatch(kind: str, payload: dict) -> None:
    """Queue for the jobs service in split mode; a thread in single-process mode."""
    from notifications.mailer import mail_async
    if mail_async():
        from core import store
        store.enqueue(kind, payload, max_attempts=1)   # retries: retry_failed_creates()
        return
    fn = {"ticket_create": create_now, "ticket_note": note_now}[kind]
    threading.Thread(target=lambda: _safe(fn, payload), daemon=True, name=kind).start()


def _safe(fn, payload):
    try:
        fn(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"{fn.__name__} failed: {exc}")


# ----------------------------------------------------------------- context

def _context(t: dict) -> dict:
    """Subject, description and attachments for one ticket."""
    system, kind = t["system"], t["kind"]
    lines = [f"System: {system}", f"Alert: {kind}", f"Raised: {t['created_at']}", ""]
    try:
        from core.reachability import current_failures
        rec = current_failures().get(system)
        if rec and kind in ("system_down", "vpn_down", "credentials", "not_reachable", "collector_down"):
            lines += [rec.get("message", ""), f"Since {rec.get('since')}", ""]
    except Exception:
        pass
    attachments: list[str] = []
    try:
        from core.status_snapshot import load_snapshot
        snap = load_snapshot(system) or {}
        crit = [m for m in (snap.get("metrics") or []) if str(m.get("status")) == "CRITICAL"]
        if crit:
            lines.append("Critical metrics (last sweep %s):" % snap.get("cycle_timestamp", "?"))
            for m in crit[:15]:
                lines.append(f"  - {m.get('name')}: {m.get('display_value')} {m.get('detail') or ''}".rstrip())
            lines.append("")
        ai = snap.get("ai_analysis") or {}
        if ai.get("root_cause"):
            lines += ["AI summary:", f"  {ai.get('root_cause')}"]
            for a in (ai.get("recommended_actions") or [])[:5]:
                lines.append(f"  * {a}")
            lines.append("")
        attachments = _screenshots(snap)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ticket context: snapshot unavailable: {exc}")
    try:
        from core import knowledge
        for hit in knowledge.suggest(system, kind, " ".join(lines))[:2]:
            lines.append(f"Last time fixed ({hit['created_at'][:10]}, {hit['created_by']}): {hit['fix'][:300]}")
    except Exception:
        pass
    try:
        from core import change_tracking
        ch = change_tracking.recent(system, 48)
        if ch:
            lines.append("Changed in the last 48 hours:")
            lines += [f"  {r['at'][5:16]}  {r['title']}" for r in ch[:15]]
            lines.append("")
    except Exception:
        pass
    from core.action_links import public_url
    base = public_url()
    if base:
        from urllib.parse import quote
        lines += ["", f"Timeline: {base}/timeline?system={quote(system)}",
                  f"Runbook:  {base}/runbook?kind={quote(kind)}&system={quote(system)}"]
    return {"subject": t.get("subject") or f"[{kind}] {system}", "description": "\n".join(lines),
            "attachments": attachments}


def _screenshots(snap: dict, limit: int = 3) -> list[str]:
    """
    Up to `limit` screenshots from the last sweep, problem T-codes first.
    Looks in the snapshot's GUI evidence (extra_data screenshot paths), then
    falls back to the newest images in today's evidence folder.
    """
    found: list[tuple[int, str]] = []
    for g in snap.get("gui_evidence") or []:
        if not isinstance(g, dict):
            continue
        extra = g.get("extra_data") or {}
        paths = []
        for k in ("screenshot_paths",):
            v = extra.get(k)
            if isinstance(v, list):
                paths += [str(x) for x in v if x]
        for k in ("screenshot_path", "screenshot"):
            v = extra.get(k) or g.get(k)
            if isinstance(v, str) and v:
                paths.append(v)
        status = str(extra.get("status") or g.get("status") or "").upper()
        rank = 0 if status == "CRITICAL" else 1 if status == "WARNING" else 2
        for p in paths:
            found.append((rank, p))
    if not found:
        try:
            from reporting.system_paths import system_evidence_root
            root = system_evidence_root(snap.get("system") or "")
            imgs = sorted((p for p in root.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            found = [(2, str(p)) for p in imgs[:limit * 2]]
        except Exception:
            pass
    found.sort(key=lambda x: x[0])
    out = []
    for _, p in found:
        try:
            if os.path.isfile(p) and os.path.getsize(p) <= 5 * 1024 * 1024 and p not in out:
                out.append(p)
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- providers

def _requests():
    import requests
    return requests


def _create_email(t: dict, ctx: dict) -> tuple[str, str]:
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from core.config_loader import get_smtp_config
    to = [e.strip() for e in (os.getenv("IBO_TICKET_EMAIL") or "").split(",") if e.strip()]
    if not to:
        raise RuntimeError("IBO_TICKET_EMAIL is not set")
    smtp = get_smtp_config(None, require_recipients=False)
    msg = MIMEMultipart("mixed")
    msg["Subject"] = ctx["subject"]
    msg["From"] = smtp["from_email"]
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(ctx["description"], "plain"))
    for p in ctx["attachments"]:
        with open(p, "rb") as fh:
            part = MIMEApplication(fh.read(), Name=os.path.basename(p))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(p)}"'
        msg.attach(part)
    # The shared transport (notifications/mailer.py): short timeout, no retry
    # on a permanent 5xx, mail-outage suspension honoured.
    from notifications import mailer
    ok, reason = mailer.transmit(msg, smtp, to, kind="ticket")
    if not ok:
        raise RuntimeError(f"ticket mail not sent: {reason}")
    return "", ""


def _create_webhook(t: dict, ctx: dict) -> tuple[str, str]:
    url = (os.getenv("IBO_TICKET_WEBHOOK_URL") or "").strip()
    if not url:
        raise RuntimeError("IBO_TICKET_WEBHOOK_URL is not set")
    body = {"event": "open", "ticket_id": t["id"], "system": t["system"], "kind": t["kind"],
            "subject": ctx["subject"], "description": ctx["description"],
            "attachments": [os.path.basename(p) for p in ctx["attachments"]]}
    headers = {"Content-Type": "application/json"}
    if os.getenv("IBO_TICKET_WEBHOOK_SECRET"):
        headers["X-IBO-Ticket-Secret"] = os.getenv("IBO_TICKET_WEBHOOK_SECRET", "")
    r = _requests().post(url, data=json.dumps(body), headers=headers, timeout=20)
    r.raise_for_status()
    try:
        ans = r.json()
    except ValueError:
        ans = {}
    url = str(ans.get("url") or "")
    return str(ans.get("id") or ""), (url if url.lower().startswith(("http://", "https://")) else "")


def _jira_auth():
    user, token = os.getenv("IBO_JIRA_USER", ""), os.getenv("IBO_JIRA_TOKEN", "")
    if not (os.getenv("IBO_JIRA_URL") and user and token and os.getenv("IBO_JIRA_PROJECT")):
        raise RuntimeError("set IBO_JIRA_URL, IBO_JIRA_USER, IBO_JIRA_TOKEN and IBO_JIRA_PROJECT")
    return (user, token)


def _create_jira(t: dict, ctx: dict) -> tuple[str, str]:
    base = os.getenv("IBO_JIRA_URL", "").rstrip("/")
    auth = _jira_auth()
    fields = {"project": {"key": os.getenv("IBO_JIRA_PROJECT")},
              "summary": ctx["subject"][:250], "description": ctx["description"],
              "issuetype": {"name": os.getenv("IBO_JIRA_ISSUETYPE", "Task")}}
    r = _requests().post(f"{base}/rest/api/2/issue", json={"fields": fields}, auth=auth, timeout=30)
    r.raise_for_status()
    key = r.json()["key"]
    for p in ctx["attachments"]:
        try:
            with open(p, "rb") as fh:
                _requests().post(f"{base}/rest/api/2/issue/{key}/attachments", auth=auth, timeout=60,
                                 headers={"X-Atlassian-Token": "no-check"},
                                 files={"file": (os.path.basename(p), fh,
                                                 mimetypes.guess_type(p)[0] or "image/png")})
        except Exception as exc:  # noqa: BLE001
            log.warning(f"jira attachment {p} failed: {exc}")
    return key, f"{base}/browse/{key}"


def _snow():
    base = os.getenv("IBO_SNOW_URL", "").rstrip("/")
    user, pw = os.getenv("IBO_SNOW_USER", ""), os.getenv("IBO_SNOW_PASSWORD", "")
    if not (base and user and pw):
        raise RuntimeError("set IBO_SNOW_URL, IBO_SNOW_USER and IBO_SNOW_PASSWORD")
    return base, (user, pw)


def _create_servicenow(t: dict, ctx: dict) -> tuple[str, str]:
    base, auth = _snow()
    body = {"short_description": ctx["subject"][:160], "description": ctx["description"],
            "urgency": "1" if t["kind"] == "system_down" else "2",
            "impact": "1" if t["kind"] == "system_down" else "2"}
    if os.getenv("IBO_SNOW_ASSIGNMENT_GROUP"):
        body["assignment_group"] = os.getenv("IBO_SNOW_ASSIGNMENT_GROUP")
    r = _requests().post(f"{base}/api/now/table/incident", json=body, auth=auth, timeout=30,
                         headers={"Accept": "application/json"})
    r.raise_for_status()
    res = r.json()["result"]
    sys_id, number = res["sys_id"], res.get("number", "")
    for p in ctx["attachments"]:
        try:
            with open(p, "rb") as fh:
                _requests().post(f"{base}/api/now/attachment/file", auth=auth, timeout=60,
                                 params={"table_name": "incident", "table_sys_id": sys_id,
                                         "file_name": os.path.basename(p)},
                                 headers={"Content-Type": mimetypes.guess_type(p)[0] or "image/png"},
                                 data=fh.read())
        except Exception as exc:  # noqa: BLE001
            log.warning(f"servicenow attachment {p} failed: {exc}")
    return sys_id, f"{base}/nav_to.do?uri=incident.do?sys_id={sys_id}" + (f"#{number}" if number else "")


_CREATE = {"email": _create_email, "webhook": _create_webhook, "jira": _create_jira,
           "servicenow": _create_servicenow}


def create_now(payload: dict) -> None:
    """Job handler: open the ticket for one tickets row."""
    t = ops_db.one("SELECT * FROM tickets WHERE id=?", (int(payload["ticket_id"]),))
    if not t or t["status"] != "opening":
        return
    ops_db.execute("UPDATE tickets SET attempts=attempts+1 WHERE id=?", (t["id"],))
    ctx = _context(t)
    try:
        ext, url = _CREATE[t["provider"]](t, ctx)
    except Exception as exc:
        ops_db.execute("UPDATE tickets SET error=? WHERE id=?", (f"{type(exc).__name__}: {exc}"[:500], t["id"]))
        log.warning(f"{t['system']}: {t['provider']} ticket failed (attempt {t['attempts'] + 1}): {exc}")
        # retry_failed_creates() (ops tick) tries again later, up to 3 attempts.
        return
    ops_db.execute("UPDATE tickets SET status='open', external_id=?, url=?, error='' WHERE id=?",
                   (ext, url, t["id"]))
    timeline.add(t["system"], "ticket", f"Ticket opened in {t['provider']}" + (f": {ext}" if ext else ""),
                 ctx["subject"], severity="warning", ref=f"ticket:{t['id']}")
    log.info(f"{t['system']}: {t['provider']} ticket {ext or '(by mail)'} opened for {t['kind']}")


def note_now(payload: dict) -> None:
    t = ops_db.one("SELECT * FROM tickets WHERE id=?", (int(payload["ticket_id"]),))
    if not t or t["status"] != "open":
        return
    text = str(payload.get("text") or "")
    if t["provider"] == "jira" and t["external_id"]:
        base = os.getenv("IBO_JIRA_URL", "").rstrip("/")
        _requests().post(f"{base}/rest/api/2/issue/{t['external_id']}/comment", json={"body": text},
                         auth=_jira_auth(), timeout=20).raise_for_status()
    elif t["provider"] == "servicenow" and t["external_id"]:
        base, auth = _snow()
        _requests().patch(f"{base}/api/now/table/incident/{t['external_id']}", json={"work_notes": text},
                          auth=auth, timeout=20).raise_for_status()
    elif t["provider"] == "webhook":
        url = (os.getenv("IBO_TICKET_WEBHOOK_URL") or "").strip()
        if url:
            _requests().post(url, json={"event": "note", "ticket_id": t["id"], "external_id": t["external_id"],
                                        "text": text}, timeout=20)


def note(system: str, kinds, text: str) -> None:
    """Add a note (e.g. 'recovered at 03:12') to any open ticket for these kinds."""
    try:
        for k in kinds:
            t = open_ticket(system, k)
            if t and t["status"] == "open" and t["provider"] != "email":
                _dispatch("ticket_note", {"ticket_id": t["id"], "text": text})
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ticket note skipped: {exc}")


# ------------------------------------------------------------------ closing

def close(ticket_id: int, by: str = "", resolution: str = "") -> dict:
    t = ops_db.one("SELECT * FROM tickets WHERE id=?", (int(ticket_id),))
    if not t:
        raise ValueError("No such ticket.")
    if t["status"] == "closed":
        return t
    by = by or timeline.actor_name()
    ops_db.execute("UPDATE tickets SET status='closed', closed_at=?, closed_by=?, resolution=? WHERE id=?",
                   (ops_db.now(), by, str(resolution or "")[:2000], t["id"]))
    timeline.add(t["system"], "ticket", f"Ticket closed{(' (' + t['external_id'] + ')') if t['external_id'] else ''}",
                 resolution, actor=by, severity="ok", ref=f"ticket:{t['id']}")
    # Closing the ticket clears the alert.
    try:
        from core import alert_control
        kinds = {t["kind"]}
        for s in alert_control.active_silences(t["system"]):
            if s["mode"] != "snooze" and s["kind"] in ("*", t["kind"]):
                ops_db.execute("UPDATE silences SET cleared_at=?, cleared_by=? WHERE id=?",
                               (ops_db.now(), f"ticket closed by {by}", s["id"]))
        p = alert_control.parked(t["system"])
        if p and (p.get("auto") or "ignored until fixed" in str(p.get("reason", ""))):
            alert_control.unpark(t["system"], by=f"ticket closed by {by}")
        timeline.add(t["system"], "silence", f"Alert state cleared for {', '.join(kinds)} (ticket closed)",
                     actor=by, severity="ok")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"ticket close: clearing alert state failed: {exc}")
    if t["kind"] == "critical":
        try:
            from core import escalation
            escalation.clear(t["system"])
        except Exception:
            pass
    if resolution and resolution.strip():
        try:
            from core import knowledge
            knowledge.add(title=t["subject"] or f"{t['kind']} on {t['system']}", fix=resolution,
                          rule=t["kind"], system=t["system"], source="ticket",
                          source_ref=t["external_id"] or str(t["id"]), by=by,
                          context=t["subject"])
        except Exception as exc:  # noqa: BLE001
            log.debug(f"ticket resolution not saved to KB: {exc}")
    return ops_db.one("SELECT * FROM tickets WHERE id=?", (t["id"],))


def _remote_state(t: dict) -> tuple[bool, str, str]:
    """(closed?, closed_by, resolution) from the ticket tool."""
    if t["provider"] == "jira":
        base = os.getenv("IBO_JIRA_URL", "").rstrip("/")
        r = _requests().get(f"{base}/rest/api/2/issue/{t['external_id']}",
                            params={"fields": "status,resolution,assignee"}, auth=_jira_auth(), timeout=20)
        r.raise_for_status()
        f = r.json().get("fields") or {}
        done = ((f.get("status") or {}).get("statusCategory") or {}).get("key") == "done"
        who = ((f.get("assignee") or {}).get("displayName")) or "Jira"
        res = (f.get("resolution") or {}).get("name") or ""
        return done, who, res
    if t["provider"] == "servicenow":
        base, auth = _snow()
        r = _requests().get(f"{base}/api/now/table/incident/{t['external_id']}", auth=auth, timeout=20,
                            params={"sysparm_fields": "state,close_notes,resolved_by,number",
                                    "sysparm_display_value": "true"})
        r.raise_for_status()
        res = r.json().get("result") or {}
        state = str(res.get("state") or "")
        done = state in ("6", "7", "Resolved", "Closed")
        rb = res.get("resolved_by")
        who = (rb.get("display_value") if isinstance(rb, dict) else rb) or "ServiceNow"
        return done, str(who), str(res.get("close_notes") or "")
    return False, "", ""


def sync() -> int:
    """Poll the ticket tool for open jira / servicenow tickets. Returns how many closed."""
    if provider() not in ("jira", "servicenow"):
        return 0
    n = 0
    for t in ops_db.rows("SELECT * FROM tickets WHERE status='open' AND provider IN ('jira','servicenow') "
                         "AND external_id!=''"):
        try:
            done, who, res = _remote_state(t)
            ops_db.execute("UPDATE tickets SET last_sync=?, error='' WHERE id=?", (ops_db.now(), t["id"]))
            if done:
                close(t["id"], by=f"{who} ({t['provider']})", resolution=res)
                n += 1
        except Exception as exc:  # noqa: BLE001
            ops_db.execute("UPDATE tickets SET last_sync=?, error=? WHERE id=?",
                           (ops_db.now(), f"{type(exc).__name__}: {exc}"[:500], t["id"]))
    return n


def retry_failed_creates() -> int:
    """
    A ticket that could not be opened (tool down, bad credentials) is tried
    again every 10 minutes, three attempts in all, then marked failed with a
    timeline line. Returns how many were re-dispatched.
    """
    from datetime import timedelta
    n = 0
    cutoff = ops_db.fmt(datetime.now() - timedelta(minutes=10))
    for t in ops_db.rows("SELECT * FROM tickets WHERE status='opening' AND error!='' AND "
                         "COALESCE(last_sync, created_at)<?", (cutoff,)):
        if int(t.get("attempts") or 0) >= 3:
            ops_db.execute("UPDATE tickets SET status='failed' WHERE id=?", (t["id"],))
            timeline.add(t["system"], "ticket", f"Ticket could NOT be opened in {t['provider']}",
                         t.get("error", ""), severity="warning", ref=f"ticket:{t['id']}")
            continue
        ops_db.execute("UPDATE tickets SET last_sync=? WHERE id=?", (ops_db.now(), t["id"]))
        _dispatch("ticket_create", {"ticket_id": t["id"]})
        n += 1
    return n


def listing(status: str = "", limit: int = 200) -> list[dict]:
    if status:
        return ops_db.rows("SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    return ops_db.rows("SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,))


def by_system() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in ops_db.rows("SELECT * FROM tickets WHERE status IN ('opening','open')"):
        out.setdefault(t["system"], []).append(t)
    return out


def describe_html(t: dict) -> str:
    label = _html.escape(t.get("external_id") or f"#{t['id']}")
    if t.get("url"):
        return f'<a href="{_html.escape(t["url"], quote=True)}">{label}</a>'
    return label

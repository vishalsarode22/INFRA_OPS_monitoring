"""
Buttons in alert mails: Snooze 1 h / Snooze 4 h / Ignore until fixed /
Acknowledge / Resume sweeps, plus links to the runbook and the timeline.

HOW A LINK IS TRUSTED
Each button is a URL to /access/action?t=<token>. The token names one
action on one system, expires (IBO_ACTION_LINK_HOURS, default 24) and is
signed with HMAC-SHA256; it works once. It needs no login, because it can
only do the one thing printed on the button -- never run a sweep, change
config or touch SAP.

Opening the link does NOT act. It shows a page with a confirm button
(and an optional "your name" field for the timeline). Mail security
scanners open every link in a message; a GET that snoozed the alert would
be pressed by the scanner before a person ever saw the mail.

WHERE THE LINKS POINT
IBO_PUBLIC_URL -- the address people use to open the dashboard, e.g.
https://ibops.infrabeat.local or http://10.1.0.50:8000. Without it the
buttons are left out (a 127.0.0.1 link only works on the VM itself) and the
Health page says so.

The signing key is IBO_ACTION_SECRET, or a random key generated once and
kept in the database (so every process on this machine agrees on it).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import quote

from utils.logger import get_logger

log = get_logger(__name__)

ACTIONS = {
    "snooze": "Snooze",
    "until_fixed": "Ignore until fixed",
    "ack": "Acknowledge",
    "unpark": "Resume sweeps",
    "mail_job_user": "Mail the user about cancelled jobs",
    "owner_mail": "Send the mail to the job owner / dump-owning team",
    "ack_report": "Acknowledge: monitoring report checked",
}


def _domains() -> set:
    raw = (os.getenv("IBO_INTERNAL_DOMAINS") or "").replace(";", ",").strip()
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def _addresses() -> set:
    """IBO_INTERNAL_ADDRESSES: single addresses that count as ours (a team member's own mailbox)."""
    raw = (os.getenv("IBO_INTERNAL_ADDRESSES") or "").replace(";", ",")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def is_internal(address) -> bool:
    """One address in IBO_INTERNAL_DOMAINS or IBO_INTERNAL_ADDRESSES."""
    a = str(address or "").strip().lower()
    if not a or "@" not in a:
        return False
    return a in _addresses() or a.rsplit("@", 1)[-1] in _domains()


def internal_only(recipients) -> bool:
    """
    True when every address is in IBO_INTERNAL_DOMAINS (comma list, e.g.
    "infrabeat.com") or IBO_INTERNAL_ADDRESSES. With neither set, no buttons
    (IBO_MAIL_BUTTONS_ALWAYS=1 overrides) -- a manager list can hold a
    customer address.
    """
    if not _domains() and not _addresses():
        # Unknown which addresses are ours: no login-free buttons unless the
        # operator says so explicitly.
        return os.getenv("IBO_MAIL_BUTTONS_ALWAYS", "0").strip().lower() in ("1", "true", "yes")
    for r in recipients or []:
        r = str(r).strip()
        if r and not is_internal(r):
            return False
    return True


def split_internal(recipients) -> tuple[list, list]:
    """(ours, others). With no domains or addresses configured nobody is ours."""
    ours, others = [], []
    for r in recipients or []:
        r = str(r).strip()
        if r:
            (ours if is_internal(r) else others).append(r)
    return ours, others


def internal_strict(recipients) -> bool:
    """For mails that can go to a customer: buttons only when the domains are known."""
    if not _domains() and not _addresses():
        return os.getenv("IBO_MAIL_BUTTONS_CRITICAL", "0").strip().lower() in ("1", "true", "yes")
    return internal_only(recipients)


def public_url() -> str:
    return (os.getenv("IBO_PUBLIC_URL") or "").strip().rstrip("/")


def _secret() -> bytes:
    env = (os.getenv("IBO_ACTION_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    from core import store
    c = store.conn()
    row = c.execute("SELECT value FROM meta WHERE key='action_secret'").fetchone()
    if row:
        return str(row["value"]).encode("utf-8")
    value = secrets.token_hex(32)
    c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('action_secret', ?)", (value,))
    row = c.execute("SELECT value FROM meta WHERE key='action_secret'").fetchone()
    return str(row["value"]).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make(action: str, system: str, kind: str = "*", minutes: int | None = None,
         ttl_hours: float | None = None, extra: dict | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    try:
        ttl = float(ttl_hours if ttl_hours is not None else os.getenv("IBO_ACTION_LINK_HOURS", "24"))
    except ValueError:
        ttl = 24.0
    body = {"a": action, "s": system, "k": kind or "*", "e": int(time.time() + ttl * 3600),
            "n": secrets.token_hex(8)}
    if minutes:
        body["m"] = int(minutes)
    for k, v in (extra or {}).items():
        if k not in body:
            body[k] = v
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()[:20]
    return f"{_b64(raw)}.{_b64(sig)}"


def verify(token: str) -> dict:
    """The token's payload. Raises ValueError with a sentence to show a person."""
    try:
        raw_b64, sig_b64 = str(token or "").split(".", 1)
        raw = _unb64(raw_b64)
        sig = _unb64(sig_b64)
    except Exception:
        raise ValueError("This link is damaged. Open the dashboard instead.")
    good = hmac.new(_secret(), raw, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(sig, good):
        raise ValueError("This link was not issued by this InfraBeatOps installation.")
    body = json.loads(raw.decode("utf-8"))
    if int(body.get("e", 0)) < time.time():
        raise ValueError("This link has expired. Use the buttons on the wall instead.")
    if body.get("a") not in ACTIONS:
        raise ValueError("Unknown action.")
    return body


def used(nonce: str) -> bool:
    from core import ops_db
    return ops_db.one("SELECT 1 AS x FROM action_tokens WHERE nonce=?", (nonce,)) is not None


def describe(body: dict) -> str:
    a, s, k = body["a"], body["s"], body.get("k", "*")
    if a == "mail_job_user":
        from core import job_user_mail
        return job_user_mail.describe(int(body.get("r") or 0))
    if a == "owner_mail":
        from core import owner_mail
        return owner_mail.describe(int(body.get("r") or 0))
    if a == "ack_report":
        return f"Record that the {s} monitoring report of {body.get('c', '')} has been checked."
    if k.startswith("metric:"):
        name = str(body.get("lb") or k[len("metric:"):])
        what = f"the '{name}' alert"
    else:
        what = "all alerts" if k == "*" else f"the {k.replace('_', ' ')} alert"
    rise = (" A rise to CRITICAL still sends a mail." if k.startswith("metric:")
            and str(body.get("l") or "").upper() == "WARNING" else "")
    target = s[len("network:"):].split(":")[0] + " (every system behind it)" if s.startswith("network:") else s
    if a == "snooze":
        mins = int(body.get("m") or 60)
        span = f"{mins // 60} hour{'s' if mins >= 120 else ''}" if mins % 60 == 0 else f"{mins} minutes"
        return f"Snooze {what} for {target} for {span}.{rise}"
    if a == "until_fixed":
        if k.startswith("metric:"):
            return (f"Ignore {what} for {target} until the value is back to normal. Other values "
                    f"and the scheduled sweeps are not affected.{rise}")
        return (f"Ignore {what} for {target} until the problem is fixed, and park its scheduled "
                f"sweeps until someone clears the flag on the wall.")
    if a == "ack":
        if k.startswith("metric:"):
            return (f"Acknowledge {what} for {target}: you are on it; repeats stop until the value "
                    f"is back to normal.{rise}")
        return f"Acknowledge {what} for {target}: you are on it; repeats stop until it is fixed."
    return f"Resume scheduled sweeps for {target} (clear the parked flag)."


def perform(token: str, actor: str = "") -> str:
    """Do what the token says, once. Returns a sentence for the result page."""
    from core import alert_control, ops_db
    body = verify(token)
    if body["a"] == "ack_report":
        cur = ops_db.db().execute("INSERT OR IGNORE INTO action_tokens(nonce, used_at, action, system) "
                                  "VALUES (?,?,?,?)", (body["n"], ops_db.now(), body["a"], body["s"]))
        if cur.rowcount != 1:
            raise ValueError("This link has already been used.")
        from core import report_gate
        who = actor.strip()[:60] if actor and actor.strip() else "someone (via e-mail link)"
        return report_gate.acknowledge(body["s"], str(body.get("c", "")), who)
    if body["a"] == "owner_mail":
        if used(body["n"]):
            raise ValueError("This link has already been used.")
        from core import owner_mail
        who = (f"{actor.strip()[:60]} (via e-mail link)" if actor and actor.strip() else "via e-mail link")
        msg = owner_mail.decide(int(body.get("r") or 0), True, by=who)
        ops_db.db().execute("INSERT OR IGNORE INTO action_tokens(nonce, used_at, action, system) "
                            "VALUES (?,?,?,?)", (body["n"], ops_db.now(), body["a"], body["s"]))
        return msg
    if body["a"] == "mail_job_user":
        # The request itself is claimed atomically (one mail for two clicks);
        # the link is spent only once the mail went out, so a mail-server
        # hiccup can be retried from the same link.
        if used(body["n"]):
            raise ValueError("This link has already been used.")
        from core import job_user_mail
        who = (f"{actor.strip()[:60]} (via e-mail link)" if actor and actor.strip() else "via e-mail link")
        msg = job_user_mail.approve(int(body.get("r") or 0), by=who)
        ops_db.db().execute("INSERT OR IGNORE INTO action_tokens(nonce, used_at, action, system) "
                            "VALUES (?,?,?,?)", (body["n"], ops_db.now(), body["a"], body["s"]))
        return msg
    # Atomic single use: the INSERT wins exactly once, even for two clicks
    # at the same instant (the ignored one has rowcount 0).
    cur = ops_db.db().execute("INSERT OR IGNORE INTO action_tokens(nonce, used_at, action, system) "
                              "VALUES (?,?,?,?)", (body["n"], ops_db.now(), body["a"], body["s"]))
    if cur.rowcount != 1:
        raise ValueError("This link has already been used.")
    who = (f"{actor.strip()[:60]} (via e-mail link)" if actor and actor.strip()
           else "via e-mail link")
    system = body["s"]
    targets = (alert_control._network_members(system) or [system]) if system.startswith("network:") else [system]
    a, kind = body["a"], body.get("k", "*")
    for t in targets:
        lvl, lb = str(body.get("l") or ""), str(body.get("lb") or "")
        if a == "snooze":
            alert_control.silence(t, kind, "snooze", minutes=int(body.get("m") or 60),
                                  reason="from the alert mail", by=who, level=lvl, label=lb)
        elif a in ("until_fixed", "ack"):
            alert_control.silence(t, kind, a, reason="from the alert mail", by=who, level=lvl, label=lb)
        elif a == "unpark":
            alert_control.unpark(t, by=who)
    log.info(f"mail link used: {a} {kind} on {', '.join(targets)} by {who}")
    return "Done. " + describe(body)


# -------------------------------------------------------------- mail footer

def _btn(url: str, label: str, colour: str) -> str:
    return (f'<a href="{html.escape(url, quote=True)}" style="display:inline-block;margin:0 6px 6px 0;'
            f'padding:7px 12px;border-radius:6px;background:{colour};color:#ffffff;'
            f'text-decoration:none;font:600 12px Segoe UI,Arial,sans-serif">{html.escape(label)}</a>')


def info_blocks(system: str, kind: str, changes: bool = True) -> str:
    """'Recent changes' and 'Last time this was fixed' for one system.
    A back-to-normal mail has no change list: nothing to investigate any more."""
    if not system or system.startswith("network:") or system == "IBOPS":
        return ""
    out = ""
    try:
        from core import change_tracking
        out += change_tracking.mail_block(system) if changes else ""
    except Exception:
        pass
    try:
        from core import knowledge
        out += knowledge.mail_block(system, kind)
    except Exception:
        pass
    return out


def footer_html(system: str, kind: str, recovered: bool = False, label: str = "", level: str = "") -> str:
    """
    Buttons, links and the change / knowledge-base blocks for one alert mail.
    Without IBO_PUBLIC_URL only the information blocks are added (a button
    to 127.0.0.1 would only work on the VM itself).
    """
    if not system:
        return ""
    name = "" if system.startswith("network:") else system
    base = public_url()
    try:
        info = info_blocks(name, "metric" if kind.startswith("metric:") else kind, changes=not recovered)
        if not base:
            return f'<div style="margin-top:18px">{info}</div>' if info else ""
        link = lambda tok: f"{base}/access/action?t={tok}"  # noqa: E731
        parts = []
        one = kind.startswith("metric:")
        x = {"lb": str(label or "")[:80], "l": str(level or "").upper()} if one else None
        if recovered:
            from core.alert_control import parked
            if parked(system) and not one:
                parts.append(_btn(link(make("unpark", system)), "Resume sweeps", "#15803d"))
        elif one:
            parts.append(_btn(link(make("ack", system, kind, extra=x)), "Acknowledge (I'm on it)", "#2563eb"))
            parts.append(_btn(link(make("snooze", system, kind, minutes=60, extra=x)), "Snooze 1 h", "#475569"))
            parts.append(_btn(link(make("snooze", system, kind, minutes=240, extra=x)), "Snooze 4 h", "#475569"))
            parts.append(_btn(link(make("until_fixed", system, kind, extra=x)), "Ignore until back to normal",
                              "#b45309"))
        else:
            parts.append(_btn(link(make("snooze", system, kind, minutes=60)), "Snooze 1 h", "#475569"))
            parts.append(_btn(link(make("snooze", system, kind, minutes=240)), "Snooze 4 h", "#475569"))
            parts.append(_btn(link(make("ack", system, kind)), "Acknowledge (I'm on it)", "#2563eb"))
            parts.append(_btn(link(make("until_fixed", system, kind)), "Ignore until fixed", "#b45309"))
        sys_q = f"&system={quote(name)}" if name else ""
        rb_kind = "metric" if one else kind
        links = (f'<a href="{base}/runbook?kind={quote(rb_kind)}{sys_q}">Runbook</a> &middot; '
                 f'<a href="{base}/timeline{("?" + sys_q[1:]) if sys_q else ""}">Timeline</a> &middot; '
                 f'<a href="{base}/wall">Live wall</a>')
        note = ("Buttons work once and expire in 24 h; each one asks you to confirm." if parts else "")
        if one and parts:
            note += (" They act on this value only. Acknowledge and Ignore end by themselves when the value "
                     "is back to normal" + ("; a rise to CRITICAL still sends a mail." if str(level).upper()
                                            == "WARNING" else "."))
        return (f'<div style="margin-top:18px;padding-top:12px;border-top:1px solid #e2e8f0">'
                f'{"".join(parts)}<div style="font:12px Segoe UI,Arial,sans-serif;color:#64748b;'
                f'margin-top:4px">{links}. {note}</div>{info}</div>')
    except Exception as exc:  # noqa: BLE001
        log.debug(f"mail footer skipped: {exc}")
        return ""


def footer_text(system: str, kind: str) -> str:
    base = public_url()
    if not base or not system:
        return ""
    try:
        link = lambda tok: f"{base}/access/action?t={tok}"  # noqa: E731
        name = "" if system.startswith("network:") else system
        return ("\n\n--\n"
                f"Snooze 1 h:          {link(make('snooze', system, kind, minutes=60))}\n"
                f"Acknowledge:         {link(make('ack', system, kind))}\n"
                f"Ignore until fixed:  {link(make('until_fixed', system, kind))}\n"
                f"Runbook:             {base}/runbook?kind={quote(kind)}{('&system=' + quote(name)) if name else ''}\n"
                f"Timeline:            {base}/timeline{('?system=' + quote(name)) if name else ''}\n")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"text footer skipped: {exc}")
        return ""

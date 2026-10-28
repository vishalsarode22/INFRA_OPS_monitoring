"""
Basis approves before a job owner or a dump-owning team is mailed
(owner's rule, 28.10.2026).

With NOTIFY_JOB_OWNERS / NOTIFY_DUMP_OWNERS on, a mail to the person who
scheduled a failed job, or to the team that owns a dump spike, is no longer
sent at once. IBOPS keeps it as a request and mails Basis:

    "IBOPS would like to mail <who> about <what>"  + a preview of the mail
    [Approve: send it]   [Do not send]

Only when someone presses Approve (a signed single-use link,
core/action_links.py action "owner_mail", or the button on the Alert history
page) does the mail go out, Basis in copy. A request expires after
IBO_OWNER_MAIL_HOURS (24). The same mail is requested once per key per day.

IBO_OWNER_MAIL_APPROVAL=0 sends them straight away again (the old behaviour).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS owner_mail_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    system      TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- job_owner | dump_owner
    key         TEXT NOT NULL,
    to_addrs    TEXT NOT NULL,
    cc_addrs    TEXT NOT NULL DEFAULT '[]',
    subject     TEXT NOT NULL,
    html        TEXT NOT NULL,
    text        TEXT NOT NULL DEFAULT '',
    what        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'waiting',  -- waiting | sending | sent | failed | rejected
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_owner_mail_key ON owner_mail_requests(key, created_at);
"""
_ready: set = set()
_lock = threading.Lock()
KINDS = {"job_owner": "job owner", "dump_owner": "dump-owning team"}


def approval_on() -> bool:
    return os.getenv("IBO_OWNER_MAIL_APPROVAL", "1").strip().lower() not in ("0", "false", "no", "off")


def ttl_hours() -> float:
    try:
        return max(1.0, float(os.getenv("IBO_OWNER_MAIL_HOURS", "24") or 24))
    except ValueError:
        return 24.0


def _db():
    from core import store
    c = store.conn()
    if store.DB_PATH not in _ready:
        with _lock:
            if store.DB_PATH not in _ready:
                c.executescript(_DDL)
                _ready.add(store.DB_PATH)
    return c


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row(rid: int) -> dict | None:
    r = _db().execute("SELECT * FROM owner_mail_requests WHERE id=?", (int(rid),)).fetchone()
    return dict(r) if r else None


def request(system: str, kind: str, key: str, to: list[str], cc: list[str], subject: str, html: str,
            text: str, what: str, basis: list[str]) -> int | None:
    """Keep the mail as a request and ask Basis. None when the same one was already asked today."""
    since = (datetime.now() - timedelta(hours=ttl_hours())).strftime("%Y-%m-%d %H:%M:%S")
    if _db().execute("SELECT 1 FROM owner_mail_requests WHERE key=? AND created_at>=?", (key, since)).fetchone():
        return None
    cur = _db().execute(
        "INSERT INTO owner_mail_requests(system, kind, key, to_addrs, cc_addrs, subject, html, text, what, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (system, kind, key, json.dumps(list(to)), json.dumps(list(cc)), subject[:300], html, text[:2000],
         what[:300], _now()))
    rid = int(cur.lastrowid)
    _ask_basis(rid, basis)
    return rid


def _ask_basis(rid: int, basis: list[str]) -> None:
    from html import escape as e
    from core import action_links
    from notifications import mail_style as ms, mailer
    r = row(rid)
    if not r or not basis:
        log.warning(f"owner mail #{rid}: no Basis address to ask -- it waits on the Alert history page")
        return
    to = json.loads(r["to_addrs"])
    url = action_links.public_url()
    button = ""
    if url and action_links.internal_only(basis):
        tok = action_links.make("owner_mail", r["system"], kind="owner_mail", ttl_hours=ttl_hours(),
                                extra={"r": rid})
        button = action_links._btn(f"{url}/access/action?t={tok}", f"Approve: send it to {', '.join(to)[:60]}",
                                   "#2563eb")
    lead = (f"IBOPS would like to mail the {e(KINDS.get(r['kind'], r['kind']))} "
            f"<b>{e(', '.join(to))}</b> about <b>{e(r['what'] or r['subject'])}</b> on <b>{e(r['system'])}</b>. "
            "<b>Nothing is sent until someone in Basis approves it.</b>")
    blocks = [ms.section("Approve", (f"<div>{button}</div>" if button else "")
                         + "<div style='font:12px Segoe UI,Arial,sans-serif;color:#64748b'>"
                           f"{'The button works once and expires in ' + str(int(ttl_hours())) + ' h. ' if button else ''}"
                           "You can also approve or decline it on the Alert history page. "
                           "If nobody approves, it is not sent.</div>"),
              ms.section("The mail that would go out",
                         "<div style='font:13px Segoe UI,Arial,sans-serif;color:#334155;margin-bottom:6px'>"
                         f"To: {e(', '.join(to))}<br>Cc: {e(', '.join(json.loads(r['cc_addrs'])) or '—')}<br>"
                         f"Subject: {e(r['subject'])}</div>"
                         f"<div style='border:1px solid #e2e8f0;border-radius:8px;padding:10px;max-height:none'>"
                         f"{_inner(r['html'])}</div>")]
    html = ms.page("INFO", f"APPROVAL NEEDED &nbsp;{ms.esc(r['system'])}",
                   f"Approve a mail to the {KINDS.get(r['kind'], r['kind'])}?", lead, blocks)
    try:
        mailer.send_html(subject=f"[InfraBeatOps] Approve: mail {', '.join(to)[:80]} about {r['what'][:80]}",
                         html=html, to=list(basis), smtp_config=_smtp(r["system"]),
                         system=r["system"], kind="owner_mail_approval", key=r["key"])
    except Exception as exc:  # noqa: BLE001
        log.warning(f"owner mail #{rid}: approval request to Basis not sent: {exc}")


def _inner(html: str) -> str:
    """The body of the stored mail, without its own <html>/<body> tags."""
    import re
    m = re.search(r"<body[^>]*>(.*)</body>", html or "", re.S | re.I)
    return m.group(1) if m else (html or "")


def _smtp(system: str) -> dict:
    from core.config_loader import get_all_systems, get_smtp_config
    cfg = next((c for c in get_all_systems() if c.get("name") == system), {"name": system})
    return get_smtp_config(cfg, require_recipients=False)


def describe(rid: int) -> str:
    r = row(rid)
    if not r:
        return "This request no longer exists."
    return (f"Send the {KINDS.get(r['kind'], r['kind'])} {', '.join(json.loads(r['to_addrs']))} the mail "
            f"about {r['what'] or r['subject']} on {r['system']}, Basis in copy.")


def waiting(systems: list | None = None) -> list[dict]:
    since = (datetime.now() - timedelta(hours=ttl_hours())).strftime("%Y-%m-%d %H:%M:%S")
    rows = [dict(r) for r in _db().execute(
        "SELECT id, system, kind, to_addrs, subject, what, created_at, status FROM owner_mail_requests "
        "WHERE status IN ('waiting', 'failed') AND created_at>=? ORDER BY id DESC LIMIT 200", (since,)).fetchall()]
    if systems is not None:
        rows = [r for r in rows if r["system"] in set(systems)]
    for r in rows:
        r["to"] = json.loads(r.pop("to_addrs"))
    return rows


def decide(rid: int, approve: bool, by: str = "") -> str:
    """Approve (send now) or decline a waiting request. Raises ValueError with a sentence."""
    from core import alert_ledger
    from notifications import mailer
    r = row(rid)
    if not r:
        raise ValueError("This request no longer exists.")
    if r["status"] not in ("waiting", "failed"):
        raise ValueError(f"This request was already {r['status']} by {r['decided_by'] or 'someone'}.")
    created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() - created > timedelta(hours=ttl_hours()):
        raise ValueError(f"This request has expired (older than {int(ttl_hours())} hours).")
    new = "sending" if approve else "rejected"
    cur = _db().execute("UPDATE owner_mail_requests SET status=?, decided_by=?, decided_at=? "
                        "WHERE id=? AND status IN ('waiting', 'failed')", (new, by or "?", _now(), int(rid)))
    if cur.rowcount != 1:
        raise ValueError("This request is already being handled.")
    if not approve:
        return f"Not sent. The {KINDS.get(r['kind'], r['kind'])} will not get this mail."
    to, cc = json.loads(r["to_addrs"]), json.loads(r["cc_addrs"])
    ok, note = False, ""
    try:
        ok = bool(mailer.send_html_now(r["subject"], r["html"], to, _smtp(r["system"]), cc=cc,
                                       text_fallback=r["text"] or r["subject"]))
        note = "" if ok else (mailer.last_reason() or "the mail server refused it")
    except Exception as exc:  # noqa: BLE001
        note = f"{type(exc).__name__}: {exc}"
    _db().execute("UPDATE owner_mail_requests SET status=?, note=? WHERE id=?",
                  ("sent" if ok else "failed", note[:300], int(rid)))
    try:
        alert_ledger.record(r["system"], r["kind"], r["key"], "INFO", r["subject"], to + cc, delivered=ok,
                            reason=f"approved by {by or '?'}" + (f"; {note}" if note else ""))
    except Exception:  # noqa: BLE001
        pass
    if not ok:
        raise ValueError(f"Approved, but the mail was not sent: {note}")
    return f"Done. {', '.join(to)} got the mail (Basis in copy)."

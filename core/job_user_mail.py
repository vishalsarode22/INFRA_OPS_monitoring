"""
"Approve: mail the user" for many cancelled jobs by one user.

When one user's jobs are cancelled many times in 15 minutes (custom alert
sap.sm37.user_cancelled_15min), Basis gets the alert with a button. Only
when a person presses it does the user get a mail listing their cancelled
jobs, with Basis in copy.

    the user's address    from their SAP user record (SU01: USR21 -> ADR6),
                          read when the alert is raised. No address = the
                          request says so and the button is not offered.
    one request           per system and user while it is waiting; new
                          cancelled jobs are added to it
    expires               24 hours after it was raised (IBO_JOB_MAIL_HOURS)
    never mailed          system accounts (DDIC, SAP*, BASIS, BATCH, ... and
                          IBO_JOB_MAIL_EXCLUDE)

The button in the mail is a signed single-use link (core/action_links.py,
action "mail_job_user"); the Alert history page has the same button for
signed-in users.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from utils.logger import get_logger

log = get_logger(__name__)

SYSTEM_ACCOUNTS = {"DDIC", "SAP*", "BASIS", "BATCH", "BGCLOUD", "TMSADM", "SAPSYS", "WF-BATCH",
                   "ALEREMOTE", "SAPJSF", "EARLYWATCH", "SOLMAN_BTC", "SM_EFWK", "SMD_RFC"}


def ttl_hours() -> float:
    try:
        return max(1.0, float(os.getenv("IBO_JOB_MAIL_HOURS", "24") or 24))
    except ValueError:
        return 24.0


def excluded(user: str) -> bool:
    u = str(user or "").strip().upper()
    extra = {x.strip().upper() for x in (os.getenv("IBO_JOB_MAIL_EXCLUDE") or "").split(",") if x.strip()}
    return (not u) or u in SYSTEM_ACCOUNTS or u in extra or u.startswith(("SAP_", "BTC", "RFC_"))


def _row(rid: int) -> dict | None:
    from core import ops_db
    return ops_db.one("SELECT * FROM job_user_requests WHERE id=?", (int(rid),))


def raise_request(system: str, user: str, email: str, jobs: list[dict]) -> dict | None:
    """
    The open request for this user (created or updated) holding the jobs not
    yet mailed. A job is never mailed twice: jobs already in a request that
    was sent (or is being sent) in the last 24 hours are left out, and when
    nothing new is left there is no request (None). None for a system account.
    """
    from core import ops_db
    if excluded(user):
        return None
    since = (datetime.now() - timedelta(hours=ttl_hours())).strftime("%Y-%m-%d %H:%M:%S")
    recent = ops_db.rows("SELECT * FROM job_user_requests WHERE system=? AND user=? AND created_at>=? "
                         "ORDER BY id DESC", (system, user, since))

    def key(j):
        return (j.get("job"), j.get("count"))
    mailed = {key(j) for r in recent if r["status"] in ("sent", "sending")
              for j in json.loads(r.get("jobs") or "[]")}
    open_row = next((r for r in recent if r["status"] in ("waiting", "failed")), None)
    if open_row:
        have = json.loads(open_row.get("jobs") or "[]")
        keys = {key(j) for j in have} | mailed
        merged = have + [j for j in jobs if key(j) not in keys]
        ops_db.execute("UPDATE job_user_requests SET jobs=?, email=CASE WHEN ?<>'' THEN ? ELSE email END "
                       "WHERE id=?", (json.dumps(merged[-200:]), email or "", email or "", open_row["id"]))
        return _row(open_row["id"])
    jobs = [j for j in jobs if key(j) not in mailed]
    if not jobs:
        return None
    ops_db.execute("INSERT INTO job_user_requests(system, user, email, jobs, created_at, status) "
                   "VALUES (?,?,?,?,?, 'waiting')",
                   (system, user, email or "", json.dumps(jobs[-200:]), ops_db.now()))
    return ops_db.one("SELECT * FROM job_user_requests WHERE system=? AND user=? ORDER BY id DESC LIMIT 1",
                      (system, user))


def waiting(systems: list | None = None) -> list[dict]:
    from core import ops_db
    since = (datetime.now() - timedelta(hours=ttl_hours())).strftime("%Y-%m-%d %H:%M:%S")
    rows = ops_db.rows("SELECT * FROM job_user_requests WHERE status IN ('waiting', 'failed') AND created_at>=? "
                       "ORDER BY id DESC LIMIT 200", (since,))
    if systems is not None:
        allowed = set(systems)
        rows = [r for r in rows if r["system"] in allowed]
    for r in rows:
        r["jobs"] = json.loads(r.get("jobs") or "[]")
    return rows


def describe(rid: int) -> str:
    r = _row(rid)
    if not r:
        return "This request no longer exists."
    n = len(json.loads(r.get("jobs") or "[]"))
    return (f"Send {r['user']} ({r['email'] or 'no address'}) a mail listing their {n} cancelled "
            f"job(s) on {r['system']}, with Basis in copy.")


def approve(rid: int, by: str = "") -> str:
    """Send the user mail for a waiting request. Returns a sentence; raises ValueError to show a person."""
    from core import alert_engine, alert_teams, ops_db
    from core.config_loader import get_all_systems, get_smtp_config
    from notifications import mailer
    r = _row(rid)
    if not r:
        raise ValueError("This request no longer exists.")
    if r["status"] not in ("waiting", "failed"):
        raise ValueError(f"This request was already {r['status']} by {r['decided_by'] or 'someone'}.")
    created = ops_db.parse(r["created_at"])
    if created and datetime.now() - created > timedelta(hours=ttl_hours()):
        raise ValueError("This request has expired (older than 24 hours).")
    if not r["email"]:
        raise ValueError(f"{r['user']} has no mail address in SAP (SU01), so nothing can be sent.")
    cfg = next((c for c in get_all_systems() if c.get("name") == r["system"]), {"name": r["system"]})
    # Claim it first: two clicks at once send one mail.
    cur = ops_db.db().execute("UPDATE job_user_requests SET status='sending', decided_by=?, decided_at=? "
                              "WHERE id=? AND status IN ('waiting', 'failed')", (by or "via e-mail link", ops_db.now(), rid))
    if cur.rowcount != 1:
        raise ValueError("This request is already being handled.")
    jobs = json.loads(r.get("jobs") or "[]")
    basis, _notes = alert_teams.resolve(cfg, [alert_teams.BASIS])
    subject, html, text = render_user_mail(r["system"], r["user"], jobs)
    ok = False
    note = ""
    if not mailer.email_enabled():
        note = "mail is switched off (IBO_ENABLE_EMAIL)"
    else:
        try:
            ok = bool(mailer.send_html_now(subject, html, [r["email"]],
                                           get_smtp_config(cfg, require_recipients=False),
                                           cc=basis, text_fallback=text))
            note = "" if ok else "the mail server refused or did not answer (see logs/email.log)"
        except Exception as exc:  # noqa: BLE001
            note = f"{type(exc).__name__}: {exc}"
    ops_db.execute("UPDATE job_user_requests SET status=?, note=? WHERE id=?",
                   ("sent" if ok else "failed", note[:300], rid))
    alert_engine.record(r["system"], "sap.sm37.user_cancelled_15min", r["user"],
                        "Cancelled jobs by one user (15 min)", "INFO", str(len(jobs)), "",
                        "user_mail", "email", ", ".join([r["email"]] + basis), ok,
                        (f"approved by {by or 'e-mail link'}" + (f"; {note}" if note else "")))
    try:
        from core import timeline
        timeline.add(r["system"], "alert", f"Cancelled-jobs mail to {r['user']}: {'sent' if ok else 'NOT sent'}",
                     f"approved by {by or 'e-mail link'}; {len(jobs)} job(s)", severity="ok" if ok else "warning")
    except Exception:  # noqa: BLE001
        pass
    if not ok:
        raise ValueError(f"The mail to {r['user']} was not sent: {note}")
    return f"Done. {r['user']} ({r['email']}) has been sent the list of {len(jobs)} cancelled job(s), Basis in copy."


def render_user_mail(system: str, user: str, jobs: list[dict]) -> tuple[str, str, str]:
    from html import escape as e
    subject = f"[InfraBeatOps] {system}: your background jobs were cancelled ({len(jobs)})"
    rows = "".join(f"<tr><td style='padding:3px 12px 3px 0'>{e(j.get('job', ''))}</td>"
                   f"<td style='padding:3px 12px 3px 0'>{e(j.get('count', ''))}</td>"
                   f"<td style='padding:3px 0'>{e(j.get('end', ''))}</td></tr>" for j in jobs[:100])
    html = ("<html><body style='font-family:Segoe UI,Arial,sans-serif;color:#0f172a'>"
            f"<p>Hello {e(user)},</p>"
            f"<p>On <b>{e(system)}</b> these background jobs scheduled by you were cancelled. "
            "Please check them in SM37 (job log) and correct or reschedule them. "
            "The Basis team is in copy if you need help.</p>"
            "<table style='border-collapse:collapse;font-size:13px'><tr>"
            "<th style='text-align:left;padding:3px 12px 3px 0'>Job</th>"
            "<th style='text-align:left;padding:3px 12px 3px 0'>Job count</th>"
            "<th style='text-align:left'>Cancelled at</th></tr>" + rows + "</table>"
            + (f"<p>... and {len(jobs) - 100} more.</p>" if len(jobs) > 100 else "")
            + "<p style='color:#64748b'>Sent by InfraBeatOps after approval by the Basis team.</p>"
            "</body></html>")
    text = f"Cancelled jobs on {system} scheduled by {user}:\n" + "\n".join(
        f"{j.get('job', '')}  {j.get('count', '')}  {j.get('end', '')}" for j in jobs[:100])
    return subject, html, text


def button_html(rid: int, recipients) -> str:
    """The approve button for the Basis mail, or a sentence when a button cannot be offered."""
    from core import action_links
    from html import escape as _e
    r = _row(rid)
    if not r:
        return ""
    # user and address come from SAP (SU01): escaped before they enter HTML
    r = dict(r, user=_e(str(r.get("user") or "")), email=_e(str(r.get("email") or "")))
    if not r["email"]:
        return (f"<p><b>{r['user']}</b> has no mail address in SAP (SU01), so the user cannot be "
                "mailed from here.</p>")
    url = action_links.public_url()
    if url and action_links.internal_only(recipients):
        token = action_links.make("mail_job_user", r["system"], kind="job_user_mail",
                                  ttl_hours=ttl_hours(), extra={"r": int(rid)})
        link = f"{url}/access/action?t={token}"
        return ("<p>" + action_links._btn(link, f"Approve: mail {r['user']}", "#2563eb")
                + f"<br><span style='color:#64748b;font-size:12px'>Sends {r['user']} ({r['email']}) the "
                  "list of their cancelled jobs, Basis in copy. Valid 24 hours, works once.</span></p>")
    return (f"<p>To mail {r['user']} ({r['email']}) the list of their cancelled jobs, approve it on the "
            "<b>Alert history</b> page of the dashboard (valid 24 hours).</p>")

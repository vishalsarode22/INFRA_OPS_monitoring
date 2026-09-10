"""
Tell the person whose job failed.

A job failure reaching only the Basis mailbox means the person who scheduled
it finds out when they look, which for a nightly job is usually the next
morning at the earliest. Telling them directly is the point of this module.

FOUR GUARDS, AND EACH EXISTS FOR A REASON

Opt-in. Off unless NOTIFY_JOB_OWNERS=true. Monitoring that starts mailing
individuals without anyone deciding to is how a tool gets switched off.

System accounts excluded. The step user of a background job is usually
BGCLOUD, BASIS, DDIC or similar. Those mailboxes are nobody's inbox, or worse,
are shared. The scheduler (SDLUNAME) is preferred over the step user for the
same reason.

Once per job per day. A job that fails on every retry would otherwise send
one mail per cycle. Anything that mails identically at 3am and 3pm gets a
mail rule pointed at it within a week, and then the real one is missed too.

Basis always copied. The owner can act on their own job; only Basis can see
whether five jobs failed for one shared reason. Sending to the owner alone
would fragment that.

WHAT IS NOT SENT
No screenshots, no system-wide metrics, no other jobs. The owner gets their
own job and nothing else -- a job failure is not a reason to hand someone a
view of production they would not otherwise have.
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.logger import get_logger

log = get_logger(__name__, "job_owner")

# Accounts that should never be mailed directly, whatever TBTCO says. These
# are service and system users: the address either does not exist or belongs
# to a team rather than a person.
_SYSTEM_ACCOUNTS = {
    "DDIC", "SAP*", "SAPCPIC", "EARLYWATCH", "TMSADM", "SOLMAN_ADMIN",
    "BGCLOUD", "BASIS", "BASIS2", "WF-BATCH", "ALEREMOTE", "REDWOOD",
    "SAP_SYSTEM", "SAPSYS", "BATCHUSER",
}


def _enabled() -> bool:
    return os.getenv("NOTIFY_JOB_OWNERS", "false").strip().lower() == "true"


def _excluded() -> set[str]:
    extra = os.getenv("JOB_OWNER_EXCLUDE", "")
    return _SYSTEM_ACCOUNTS | {u.strip().upper()
                               for u in extra.split(",") if u.strip()}


def _state_path() -> str:
    from utils.paths import BASE_DIR
    return os.path.join(BASE_DIR, "config", "job_owner_notified.json")


def _load_state() -> dict:
    import json
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    import json
    try:
        # Yesterday's keys are dead weight; a job failing daily would grow
        # this file forever otherwise.
        today = datetime.now().strftime("%Y-%m-%d")
        state = {k: v for k, v in state.items() if v == today}
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
    except Exception as exc:
        log.warning(f"Could not save job-owner notify state: {exc}")


def _already_sent(key: str) -> bool:
    return _load_state().get(key) == datetime.now().strftime("%Y-%m-%d")


def _mark_sent(key: str) -> None:
    state = _load_state()
    state[key] = datetime.now().strftime("%Y-%m-%d")
    _save_state(state)


def _body(system: str, job: dict, reason: str) -> tuple[str, str]:
    is_cancelled = reason == "cancelled"
    subject = (f"[{system}] Your background job "
               f"{'was cancelled' if is_cancelled else 'is still running'}: "
               f"{job['job']}")

    if is_cancelled and job.get("never_started"):
        # The single most useful sentence in the mail. A job that ended at its
        # start time is not an ABAP failure, and sending someone to ST22 for
        # it wastes their morning.
        what = ("It ended at the moment it was due to start, which means it "
                "never actually ran. That is normally an authorisation "
                "problem, a variant that no longer exists, or no free "
                "background work process -- not an error inside the report.")
        next_steps = [
            "SM37: open the job and read the job log.",
            "SU53 for the step user, immediately after a failed run.",
            "Check the variant named in the job step still exists.",
        ]
    elif is_cancelled:
        what = (f"It ran for {job.get('duration_text')} and then cancelled, "
                f"so it failed while executing. There is usually a dump or a "
                f"job log entry at the moment it stopped.")
        next_steps = [
            f"ST22: look for a dump at {job.get('ended_at')}.",
            "SM37: open the job log for the cancelled run.",
            "If it fails at the same point each day, the report needs "
            "attention rather than a re-run.",
        ]
    else:
        what = (f"It has been running for {job.get('duration_text')}, which is "
                f"longer than expected. It may be working normally on a large "
                f"volume, or it may be blocked.")
        next_steps = [
            "SM50 / SM66: check whether the work process is active or waiting.",
            "SM12: if it is waiting on a lock, note who holds it.",
            "Do not cancel it without checking -- an interrupted update can "
            "leave a document half-posted.",
        ]

    lines = "".join(f"<li>{step}</li>" for step in next_steps)

    html = f"""<!doctype html><html><body style="margin:0;padding:22px;
      background:#f4f4f7;font-family:-apple-system,'Segoe UI',Arial,sans-serif;">
      <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:6px;
                  border:1px solid #d0d7de;padding:20px 22px;">
        <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;
                    color:#57606a;">InfraBeatOps · {system}</div>
        <h2 style="margin:6px 0 14px;font-size:19px;color:#1f2328;">
          {job['job']}</h2>

        <table style="width:100%;border-collapse:collapse;font-size:13.5px;
                      margin-bottom:16px;">
          <tr><td style="padding:4px 0;color:#57606a;width:130px;">Scheduled by</td>
              <td style="padding:4px 0;">{job.get('scheduled_by') or '—'}</td></tr>
          <tr><td style="padding:4px 0;color:#57606a;">Ran as</td>
              <td style="padding:4px 0;">{job.get('user') or '—'}</td></tr>
          <tr><td style="padding:4px 0;color:#57606a;">Started</td>
              <td style="padding:4px 0;">{job.get('started_at') or '—'}</td></tr>
          <tr><td style="padding:4px 0;color:#57606a;">
              {'Ended' if is_cancelled else 'Running for'}</td>
              <td style="padding:4px 0;">{job.get('ended_at')
                  if is_cancelled else job.get('duration_text')}</td></tr>
          <tr><td style="padding:4px 0;color:#57606a;">Duration</td>
              <td style="padding:4px 0;">{job.get('duration_text') or '—'}</td></tr>
          <tr><td style="padding:4px 0;color:#57606a;">Job id</td>
              <td style="padding:4px 0;font-family:Consolas,monospace;">
                {job.get('job_count') or '—'}</td></tr>
        </table>

        <p style="font-size:13.5px;line-height:1.6;color:#1f2328;">{what}</p>

        <div style="font-size:11px;font-weight:700;letter-spacing:.05em;
                    text-transform:uppercase;color:#57606a;margin:16px 0 6px;">
          What to check</div>
        <ol style="font-size:13.5px;line-height:1.65;padding-left:20px;
                   margin:0;color:#1f2328;">{lines}</ol>

        <p style="font-size:12px;color:#57606a;margin-top:18px;line-height:1.55;">
          Sent because you scheduled this job. Your Basis team is copied.
          You will not get another mail about this job today, even if it
          fails again.
        </p>
      </div></body></html>"""

    text = (f"{system}: {job['job']}\n"
            f"Scheduled by {job.get('scheduled_by') or '-'}, "
            f"ran as {job.get('user') or '-'}\n"
            f"Started {job.get('started_at') or '-'}, "
            f"duration {job.get('duration_text') or '-'}\n\n{what}\n\n"
            + "\n".join(f"- {s}" for s in next_steps))

    return subject, html if True else text


def notify_job_owners(system: str, job_detail: dict, smtp_config: dict,
                      basis_recipients=None) -> dict:
    """
    Mail the owner of each cancelled or long-running job. Never raises.

    Returns a summary including who could not be reached -- an owner with no
    resolvable address is worth knowing about, since it means those failures
    reach nobody but Basis.
    """
    if not _enabled():
        return {"sent": 0, "reason": "NOTIFY_JOB_OWNERS is not enabled"}

    excluded = _excluded()
    basis = list(basis_recipients or smtp_config.get("to_emails") or [])

    candidates = [(j, "cancelled") for j in (job_detail.get("cancelled") or [])]
    candidates += [(j, "long_running") for j in (job_detail.get("long_running") or [])]

    sent, skipped, unreachable = 0, [], []

    for job, reason in candidates:
        owner = (job.get("owner") or "").upper()
        address = job.get("owner_email")

        if not owner or owner in excluded:
            skipped.append(f"{job['job']}: owner {owner or 'unknown'} is a "
                           f"system account or unset")
            continue
        if not address:
            unreachable.append(f"{job['job']}: no mailbox for {owner}")
            continue

        key = f"{system}:{job['job']}:{job.get('job_count') or ''}:{reason}"
        if _already_sent(key):
            skipped.append(f"{job['job']}: already notified today")
            continue

        subject, html = _body(system, job, reason)
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = smtp_config["from_email"]
        message["To"] = address
        if basis:
            message["Cc"] = ", ".join(basis)
        message.attach(MIMEText(f"{job['job']} on {system}. "
                                f"This message needs an HTML mail client.", "plain"))
        message.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(smtp_config["host"], smtp_config["port"],
                              timeout=60) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_config["username"], smtp_config["password"])
                server.sendmail(smtp_config["from_email"],
                                [address] + basis, message.as_string())
            _mark_sent(key)
            sent += 1
            log.info(f"Job owner notified: {owner} <{address}> about "
                     f"{job['job']} ({reason})")
        except Exception as exc:
            log.warning(f"Could not notify {owner} about {job['job']}: {exc}")

    if unreachable:
        # Logged as a warning: these failures reach nobody but Basis, and that
        # is a gap someone should close rather than a normal outcome.
        log.warning(f"{system}: no mailbox for {len(unreachable)} job owner(s) "
                    f"-- {'; '.join(unreachable[:5])}")

    return {"sent": sent, "skipped": skipped, "unreachable": unreachable}

"""
How every mail leaves this tool: one SMTP transport, one set of rules.

Every sender (per-value alerts, VPN / DOWN / credential alerts, failure
notices, the scheduled report, RCA, job / dump owner mails) ends in
transmit(). So the SMTP details, the To/Cc envelope rule, the timeouts, the
retry rule and the "never raise" promise live in one place.

Cc must go on both the header and the envelope. Header-only Cc shows the
address in the client while the server never delivers to it -- the worst
failure, because it looks like it worked.

RETRIES AND TIMEOUTS
    socket timeout          IBO_SMTP_TIMEOUT seconds (default 20)
    transient failure       (connection refused / dropped, timeout, 4xx)
                            retried IBO_SMTP_RETRIES times (default 1)
    permanent failure 5xx   NEVER retried: the same mail gets the same answer.

MAIL OUTAGE (Gmail "550 5.4.5 Daily user sending limit exceeded")
    A quota answer suspends the sending account, persisted on disk, for
    IBO_MAIL_SUSPEND_MINUTES (default 60; Gmail's limit is a rolling 24 h).
    While suspended nothing is sent from that account (the gate records
    "suppressed-mail-outage"); when the time is up ONE mail is let through as
    a probe -- success lifts the suspension, another quota answer renews it.
    CRITICAL, SYSTEM DOWN, credentials and VPN mail -- and the scheduled
    client report -- are never just dropped: they go through the secondary
    account (IBO_SECONDARY_SMTP_ACCOUNT=<account id in
    config/delivery_accounts.yaml>) when one is set, then through the main
    account anyway (the suspension is only a guess; a success lifts it).
    Critical kinds also get a WhatsApp copy when WhatsApp is configured
    (core/alert_ledger.py). A report that still fails is re-queued by
    core/outbox.py for after the suspension (IBO_REPORT_OUTAGE_RETRIES).

NOT BLOCKING THE CALLER
    deliver() / enqueue_mail() take the gate decision now and hand the
    sending to the jobs service queue (IBO_MAIL_ASYNC=1, split services) or
    to an in-process background worker (single process). They return at once.

THE CLIENT
    For every internal kind (anything but the report and the mails that go
    to a SAP user by design) the client's addresses are removed from To and
    Cc here, whoever built the list (core/recipients.strip_client).
"""
from __future__ import annotations

import os
import queue
import re
import smtplib
import socket
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.logger import get_logger

log = get_logger(__name__)

_tls = threading.local()           # last_reason of this thread's last send


def email_enabled() -> bool:
    return str(os.getenv("IBO_ENABLE_EMAIL", "1")).strip().lower() not in ("0", "false", "no", "off")


def mail_async() -> bool:
    """
    Queue for the jobs service instead of sending inline.

    Default: on when this process is one of the split services (IBO_ROLE=ui
    or collector, so a jobs runner exists), off in the single-process
    IBO_ROLE=all deployment where nothing would drain the queue. Override
    with IBO_MAIL_ASYNC=0/1.
    """
    raw = os.getenv("IBO_MAIL_ASYNC")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")
    return (os.getenv("IBO_ROLE", "all") or "all").strip().lower() in ("ui", "collector", "jobs")


def last_reason() -> str:
    """Why this thread's last send failed ('' after a success)."""
    return getattr(_tls, "reason", "") or ""


def _set_reason(text: str) -> None:
    _tls.reason = text or ""


def _env_float(name: str, default: float, lo: float = 0.0) -> float:
    try:
        return max(lo, float(os.getenv(name, str(default)) or default))
    except ValueError:
        return default


def smtp_timeout() -> float:
    return _env_float("IBO_SMTP_TIMEOUT", 20.0, 3.0)


def smtp_retries() -> int:
    return int(_env_float("IBO_SMTP_RETRIES", 1, 0))


def suspend_minutes() -> float:
    return _env_float("IBO_MAIL_SUSPEND_MINUTES", 60.0, 1.0)


# Kinds that still go out while the primary account is suspended (secondary
# account first, then the main account anyway, plus a WhatsApp copy).
CRITICAL_KINDS = frozenset({"system_down", "critical", "monitor_down", "credentials", "vpn_down"})
# Kinds that are never dropped by a suspension: tried like critical mail, and
# re-queued by core/outbox.py when they still cannot be sent.
MUST_SEND_KINDS = frozenset({"report"})


def is_critical(kind: str = "", severity: str = "") -> bool:
    return str(kind or "") in CRITICAL_KINDS or str(severity or "").upper() == "CRITICAL"


def must_send(kind: str = "", severity: str = "") -> bool:
    """Critical mail and the client report: not suppressed by a suspension."""
    return is_critical(kind, severity) or str(kind or "") in MUST_SEND_KINDS


# ------------------------------------------------------------- classification

_QUOTA = re.compile(r"5\.4\.5|daily (user )?sending (quota|limit)|sending limit exceeded|"
                    r"quota exceeded|exceeded .*quota|message limit|too many (messages|emails)",
                    re.IGNORECASE)


class _Outcome:
    __slots__ = ("ok", "reason", "permanent", "quota")

    def __init__(self, ok=False, reason="", permanent=False, quota=False):
        self.ok, self.reason, self.permanent, self.quota = ok, reason, permanent, quota


def classify(exc: BaseException) -> _Outcome:
    """Permanent (5xx), quota (5.4.5 / sending limit) or transient (anything else)."""
    from notifications.graph_mail import GraphError
    if isinstance(exc, GraphError):
        kind = "permanent" if exc.permanent else "transient"
        return _Outcome(False, f"{kind} {exc}"[:300], permanent=exc.permanent, quota=exc.quota)
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", b"")
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _m in (exc.recipients or {}).values()]
        msgs = [m.decode("utf-8", "replace") if isinstance(m, (bytes, bytearray)) else str(m)
                for _c, m in (exc.recipients or {}).values()]
        code = min(codes) if codes else None
        text = "; ".join(msgs)
    full = f"{type(exc).__name__}: {code or ''} {text or exc}".strip()
    if isinstance(exc, socket.gaierror):
        # The SMTP host name does not resolve: a setting, not a hiccup.
        return _Outcome(False, f"permanent {full} (SMTP host name not found)"[:300], permanent=True)
    quota = bool(_QUOTA.search(text or str(exc)))
    if isinstance(code, int) and code >= 500:
        return _Outcome(False, f"permanent {full}"[:300], permanent=True, quota=quota)
    if quota and isinstance(code, int) and code >= 400:
        # "421 4.7.0 too many messages" style: a rate limit, not the daily cap
        return _Outcome(False, f"transient {full}"[:300], permanent=False, quota=False)
    return _Outcome(False, f"transient {full}"[:300])


# ------------------------------------------------------------- suspension

_mem_suspend: dict = {}
_suspend_lock = threading.Lock()


def _acct_key(smtp_config: dict) -> str:
    return (str(smtp_config.get("account") or "")
            or f"{smtp_config.get('username') or ''}@{smtp_config.get('host') or ''}")


def _meta_get(key: str):
    try:
        from core import ops_db
        row = ops_db.one("SELECT value FROM meta WHERE key=?", (key,))
        return ops_db.loads(row["value"], None) if row else None
    except Exception:  # noqa: BLE001
        return _mem_suspend.get(key)


def _meta_set(key: str, value) -> None:
    _mem_suspend[key] = value
    try:
        from core import ops_db
        if value is None:
            ops_db.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            ops_db.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                           "value=excluded.value", (key, ops_db.dumps(value)))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"mail suspension not persisted: {exc}")


def _skey(smtp_config: dict) -> str:
    return "mail_suspended:" + _acct_key(smtp_config)


def suspension(smtp_config: dict | None = None) -> dict | None:
    """{"until", "since", "reason"} while the account is suspended, else None."""
    if smtp_config is None:
        smtp_config = _default_smtp()
    rec = _meta_get(_skey(smtp_config))
    if not rec:
        return None
    try:
        until = datetime.strptime(str(rec.get("until"))[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dict(rec, until_dt=until, active=until > datetime.now())


def mail_suspended(smtp_config: dict | None = None) -> str:
    """'' when the account may send now (or may send its one probe); else why not."""
    rec = suspension(smtp_config)
    if rec and rec.get("active"):
        return (f"mail suspended until {rec['until_dt']:%Y-%m-%d %H:%M} "
                f"({str(rec.get('reason') or 'quota')[:120]})")
    return ""


def suspend(smtp_config: dict, reason: str) -> None:
    now = datetime.now()
    prev = suspension(smtp_config) or {}
    _meta_set(_skey(smtp_config), {"until": (now + timedelta(minutes=suspend_minutes())).strftime(
        "%Y-%m-%d %H:%M:%S"), "since": prev.get("since") or now.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": str(reason)[:200]})
    log.error(f"MAIL SUSPENDED for {_acct_key(smtp_config)} for {suspend_minutes():.0f} min: {reason}")


def clear_suspension(smtp_config: dict) -> None:
    if _meta_get(_skey(smtp_config)):
        log.warning(f"mail suspension lifted for {_acct_key(smtp_config)}")
    _meta_set(_skey(smtp_config), None)


def _take_probe(smtp_config: dict) -> bool:
    """A suspension that ran out: let exactly one send through (renew pessimistically first)."""
    with _suspend_lock:
        rec = suspension(smtp_config)
        if not rec:
            return True
        if rec.get("active"):
            return False
        suspend(smtp_config, f"probe after: {rec.get('reason') or 'quota'}")
        return True


def _default_smtp() -> dict:
    try:
        from core.config_loader import get_smtp_config
        return get_smtp_config(None, require_recipients=False)
    except Exception:  # noqa: BLE001
        return {}


def secondary_smtp(system: str | None = None) -> dict | None:
    """The secondary sending account (IBO_SECONDARY_SMTP_ACCOUNT), or None."""
    aid = (os.getenv("IBO_SECONDARY_SMTP_ACCOUNT") or "").strip()
    if not aid:
        return None
    try:
        from core import delivery_accounts
        if not any(a["id"] == aid for a in delivery_accounts.load_accounts()["email"]):
            log.warning(f"IBO_SECONDARY_SMTP_ACCOUNT={aid}: no such account in delivery_accounts.yaml")
            return None
        cfg = delivery_accounts.resolve_email_account(aid, system)
        return cfg if cfg.get("host") else None
    except Exception as exc:  # noqa: BLE001
        log.warning(f"secondary SMTP account not usable: {exc}")
        return None


# ------------------------------------------------------------- transport

_FREE_MAIL = ("gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
              "live.com", "rediffmail.com", "icloud.com", "aol.com", "proton.me", "protonmail.com")


def personal_sender(smtp_config: dict) -> str:
    """The sender/login if it is a free personal mailbox (gmail.com ...), else ''.
    IBO_ALLOW_PERSONAL_SENDER=1 allows it (tests on a laptop only)."""
    if (os.getenv("IBO_ALLOW_PERSONAL_SENDER") or "").strip().lower() in ("1", "true", "yes"):
        return ""
    for v in (smtp_config.get("from_email"), smtp_config.get("username")):
        addr = str(v or "").strip().lower()
        if "@" in addr and addr.rsplit("@", 1)[1].strip(">") in _FREE_MAIL:
            return addr.strip("<>")
    return ""


def _smtp_once(msg, smtp_config: dict, envelope: list[str]) -> None:
    from notifications import graph_mail
    if graph_mail.enabled():
        # IBO_MAIL_TRANSPORT=graph: every mail leaves through Microsoft Graph as
        # the one sender mailbox, whatever account the caller resolved.
        graph_mail.send(msg, envelope)
        return
    personal = personal_sender(smtp_config)
    if personal:
        # Never from someone's own mailbox: alerts must come from the
        # company sender (ibops@infrabeat.com via Graph).
        raise smtplib.SMTPSenderRefused(550, f"refused by IBOPS: {personal} is a personal mailbox; "
                                              f"set IBO_MAIL_TRANSPORT=graph (IBO_GRAPH_*) in .env",
                                        personal)
    host, port = smtp_config["host"], int(smtp_config.get("port") or 587)
    if port == 465 and smtp_config.get("use_tls", True):
        server = smtplib.SMTP_SSL(host, port, timeout=smtp_timeout())
    else:
        server = smtplib.SMTP(host, port, timeout=smtp_timeout())
    with server:
        if port != 465 and smtp_config.get("use_tls", True) not in (False, "false", "0", 0, "no"):
            server.starttls()
        if smtp_config.get("username"):
            server.login(smtp_config["username"], smtp_config.get("password") or "")
        server.sendmail(smtp_config["from_email"], envelope, msg.as_string())


def _try_account(msg, smtp_config: dict, envelope: list[str]) -> _Outcome:
    tries = smtp_retries() + 1
    out = _Outcome(False, "not attempted")
    for attempt in range(1, tries + 1):
        try:
            _smtp_once(msg, smtp_config, envelope)
            return _Outcome(True, "")
        except Exception as exc:  # noqa: BLE001
            out = classify(exc)
            if out.permanent or attempt >= tries:
                return out
            log.warning(f"SMTP attempt {attempt}/{tries} failed ({out.reason}); retrying once")
            time.sleep(min(2.0, smtp_timeout()))
    return out


def transmit(msg, smtp_config: dict, envelope: list[str], kind: str = "",
             severity: str = "") -> tuple[bool, str]:
    """
    Send one prepared MIME message. (ok, reason). Never raises.

    reason starts with "suppressed-mail-outage" when the account is suspended,
    "permanent" for a 5xx answer, "transient" otherwise.
    """
    envelope = [e for e in (envelope or []) if e]
    if not envelope:
        return False, "no recipients"
    if not smtp_config or not smtp_config.get("host"):
        return False, "no SMTP host configured"
    important = must_send(kind, severity)
    primary_ok = _take_probe(smtp_config)
    main_tried = False
    if primary_ok:
        main_tried = True
        out = _try_account(msg, smtp_config, envelope)
        if out.ok:
            clear_suspension(smtp_config)
            return True, ""
        if out.quota:
            suspend(smtp_config, out.reason)
        elif suspension(smtp_config):
            # The probe failed for another reason: keep the suspension running.
            pass
        if not (out.quota and important):
            return False, out.reason
        first_reason = out.reason
    else:
        first_reason = f"suppressed-mail-outage: {mail_suspended(smtp_config)}"
        if not important:
            return False, first_reason

    # Critical mail / the report while the main account is suspended:
    # 1. the secondary account, when one is configured and not suspended;
    reasons = [first_reason]
    main_from = msg.get("From")
    from notifications import graph_mail
    second = None if graph_mail.enabled() else secondary_smtp()
    if not second or _acct_key(second) == _acct_key(smtp_config):
        reasons.append("no secondary account (IBO_SECONDARY_SMTP_ACCOUNT)")
    elif mail_suspended(second):
        reasons.append(f"secondary also {mail_suspended(second)}")
    else:
        try:
            del msg["From"]
            msg["From"] = second["from_email"]
        except Exception:  # noqa: BLE001
            pass
        out2 = _try_account(msg, second, envelope)
        if out2.ok:
            log.warning(f"mail sent through the secondary account {_acct_key(second)} ({first_reason})")
            return True, ""
        if out2.quota:
            suspend(second, out2.reason)
        reasons.append(f"secondary: {out2.reason}")
        try:
            del msg["From"]
            msg["From"] = main_from or smtp_config.get("from_email", "")
        except Exception:  # noqa: BLE001
            pass
    # 2. the main account anyway: the suspension is a guess about Gmail's
    #    rolling 24 h window. A success lifts it.
    if not main_tried:
        out3 = _try_account(msg, smtp_config, envelope)
        if out3.ok:
            log.warning(f"{kind or 'mail'} sent through the suspended main account "
                        f"{_acct_key(smtp_config)}; suspension lifted.")
            clear_suspension(smtp_config)
            return True, ""
        if out3.quota:
            suspend(smtp_config, out3.reason)
        reasons.append(f"main account: {out3.reason}")
    return False, "; ".join(reasons)


# ------------------------------------------------------------- client

def _cfg_for(system: str) -> dict:
    try:
        from core.config_loader import get_all_systems
        return next((s for s in get_all_systems() if s.get("name") == system), None) or {"name": system}
    except Exception:  # noqa: BLE001
        return {"name": system}


def excluded_addresses() -> set[str]:
    """IBO_MAIL_EXCLUDE: addresses IBOPS never mails, whatever list they are in."""
    raw = os.getenv("IBO_MAIL_EXCLUDE") or ""
    return {a.strip().lower() for a in re.split(r"[,;\s]+", raw) if "@" in a}


def without_client(to, cc, system: str, kind: str) -> tuple[list[str], list[str], list[str]]:
    """(to, cc, removed) -- IBO_MAIL_EXCLUDE addresses always, and the client's
    addresses out of an internal kind."""
    to = [t for t in (to or []) if t]
    cc = [c for c in (cc or []) if c]
    never = excluded_addresses()
    if never:
        gone = [a for a in to + cc if a.strip().lower() in never]
        if gone:
            to = [a for a in to if a.strip().lower() not in never]
            cc = [a for a in cc if a.strip().lower() not in never]
            log.info(f"{system or 'mail'}: {kind or 'mail'}: left out (IBO_MAIL_EXCLUDE): {gone}")
    if not kind:
        return to, cc, []
    from core import recipients
    if not recipients.is_internal_kind(kind):
        return to, cc, []
    cfg = _cfg_for(system.split(":", 1)[1] if system.startswith("network:") else system) if system else {}
    to2, r1 = recipients.strip_client(to, cfg)
    cc2, r2 = recipients.strip_client(cc, cfg)
    if not to2 and cc2:
        to2, cc2 = cc2[:1], cc2[1:]
    removed = r1 + r2
    if removed:
        log.warning(f"{system}: {kind} mail: client address(es) removed -- internal alerts never go "
                    f"to the client: {removed}")
    return to2, cc2, removed


# ------------------------------------------------------------- public API

def with_action_footer(html: str, system: str, kind: str, recipients=None) -> str:
    """
    Add the Snooze / Acknowledge / Ignore-until-fixed buttons, runbook and
    timeline links (core/action_links.py) to an alert mail. Only when the
    mail is about a system and an alert kind, and only once.
    """
    if not (system and kind) or "ibo-actions" in (html or ""):
        return html
    try:
        from core.action_links import footer_html, internal_only
        if recipients is not None and not internal_only(recipients):
            return html
        from core.alert_control import RECOVERY_KINDS
        footer = footer_html(system, kind, recovered=kind in RECOVERY_KINDS)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"action footer skipped: {exc}")
        return html
    if not footer:
        return html
    from notifications.mail_style import insert
    return insert(html or "", f"<!-- ibo-actions -->{footer}")


def send_html(subject: str, html: str, to: list[str], smtp_config: dict,
              cc: list[str] | None = None, text_fallback: str | None = None,
              system: str = "", kind: str = "", key=None, min_interval_s: float = 0,
              daily_cap: int | None = None, severity: str = "") -> bool:
    """
    Send, or queue for the jobs service (IBO_MAIL_ASYNC=1).

    The gate decision (core/alert_ledger.should_send: policy, maintenance /
    snooze, mail outage, repeat, caps) is taken HERE, at queue time, so a
    held mail never reaches the queue. The queued job carries the system
    name, not the SMTP password: the runner resolves credentials itself.
    Returns True when the mail was sent or queued.
    """
    from core import alert_ledger
    to, cc, _removed = without_client(to, cc, system, kind)
    html = with_action_footer(html, system, kind, list(to or []) + list(cc or []))
    if mail_async():
        if not to:
            log.warning(f"mail not queued, no recipients: {subject}")
            return False
        if not email_enabled():
            log.info(f"mail suppressed (IBO_ENABLE_EMAIL=0): {subject} -> {to}")
            return False
        if system and kind:
            ok, why = alert_ledger.should_send(system, kind, key, min_interval_s, daily_cap,
                                               severity=severity)
            if not ok:
                log.info(f"{system}: {kind} mail held: {why}")
                return False
        from core import store
        job_id = store.enqueue("mail", {"subject": subject, "html": html, "to": to, "cc": cc,
                                        "text": text_fallback, "system": system, "kind": kind,
                                        "key": key, "severity": severity})
        log.info(f"mail queued (job {job_id}): {subject} -> {to}")
        return True
    return send_html_now(subject, html, to, smtp_config, cc, text_fallback, system, kind, key,
                         min_interval_s, daily_cap, severity)


def send_html_now(subject: str, html: str, to: list[str], smtp_config: dict,
                  cc: list[str] | None = None, text_fallback: str | None = None,
                  system: str = "", kind: str = "", key=None, min_interval_s: float = 0,
                  daily_cap: int | None = None, severity: str = "",
                  reply_to: list[str] | None = None) -> bool:
    """
    Send inline; return True on success. Logs and returns False on failure
    (the reason is in last_reason()).
    `reply_to`: addresses a reply goes to.

    With `system` and `kind` set and min_interval_s/daily_cap given, the
    gate decides whether this may go out and the ledger records the outcome.
    (The jobs runner passes no interval: that decision was taken at queue
    time and must not be taken twice.)
    """
    from core import alert_ledger
    _set_reason("")
    to, cc, _removed = without_client(to, cc, system, kind)
    if not to:
        log.warning(f"mail not sent, no recipients: {subject}")
        _set_reason("no recipients" + (" (client addresses removed)" if _removed else ""))
        return False
    if not email_enabled():
        log.info(f"mail suppressed (IBO_ENABLE_EMAIL=0): {subject} -> {to}")
        _set_reason("mail is switched off (IBO_ENABLE_EMAIL)")
        return False
    if system and kind and (min_interval_s or daily_cap is not None):
        ok, why = alert_ledger.should_send(system, kind, key, min_interval_s, daily_cap,
                                           severity=severity)
        if not ok:
            log.info(f"{system}: {kind} mail held: {why}")
            _set_reason(why)
            return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_config["from_email"]
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        reply_to = [r for r in (reply_to or []) if r]
        if reply_to:
            msg["Reply-To"] = ", ".join(reply_to)
        if text_fallback:
            msg.attach(MIMEText(text_fallback, "plain"))
        msg.attach(MIMEText(html, "html"))
        ok, reason = transmit(msg, smtp_config, to + cc, kind=kind, severity=severity)
    except Exception as exc:  # noqa: BLE001 -- a bad config dict must not raise
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    if ok:
        log.info(f"mail sent: {subject} -> {to}{' cc ' + str(cc) if cc else ''}")
        if system and kind:
            alert_ledger.record(system, kind, key, severity, subject, to + cc)
        return True
    log.error(f"mail failed: {subject}: {reason}")
    _set_reason(reason)
    if system and kind:
        alert_ledger.record(system, kind, key, severity, subject, to + cc,
                            delivered=False, reason=str(reason)[:200])
    return False


# send_message() kinds whose callers do not write their own Alert history
# row (critical, dump_owner and rca record theirs): the scheduled report and
# the diagnostic (RCA) alert. Recorded here so the Monitoring log and Alert
# history show every report mail -- sent, suppressed or failed -- with its
# To, Cc and subject.
RECORDED_BY_SEND_MESSAGE = frozenset({"report", "final_report", "diagnostic"})


def _addresses(value) -> list[str]:
    return [a.strip() for a in str(value or "").split(",") if a.strip()]


def _record_message(msg, system: str, kind: str, severity: str, ok: bool, envelope) -> None:
    """One Alert history (alert_sends) row for a send_message() outcome. Never raises."""
    try:
        to = _addresses(msg.get("To"))
        cc = _addresses(msg.get("Cc"))
        if not to and not cc:
            to = [e for e in (envelope or []) if e]
        who = ", ".join(to) + (f" | cc: {', '.join(cc)}" if cc else "")
        subject = str(msg.get("Subject", "") or kind)
        reason = last_reason()
        if ok:
            decision, note = "sent", subject
        elif reason.startswith("mail is switched off") or reason.startswith("no recipients"):
            decision, note = "suppressed", f"suppressed: {reason}"
        else:
            decision, note = "send failed", f"send failed: {reason or 'see logs/email.log'}"
        from core import alert_ledger
        alert_ledger.history(system, kind, subject, severity or "INFO", ok, note,
                             recipients=who, decision=decision)
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must never fail a send
        log.debug(f"mail history row not written ({kind}): {exc}")


def send_message(msg, smtp_config: dict, envelope: list[str], system: str = "",
                 kind: str = "", severity: str = "") -> bool:
    """
    A prepared MIME message (attachments) through the same transport. Never raises.

    The client's addresses are taken out of every internal kind (everything
    but the report and the mails that go to a SAP user by design), from the
    envelope AND the To/Cc headers, exactly as deliver() does.

    The report and diagnostic kinds (RECORDED_BY_SEND_MESSAGE) get their
    Alert history row here; the other callers write their own.
    """
    ok = _send_message(msg, smtp_config, envelope, system, kind, severity)
    if system and kind in RECORDED_BY_SEND_MESSAGE:
        _record_message(msg, system, kind, severity, ok, envelope)
    return ok


def _send_message(msg, smtp_config: dict, envelope: list[str], system: str = "",
                  kind: str = "", severity: str = "") -> bool:
    _set_reason("")
    if not email_enabled():
        _set_reason("mail is switched off (IBO_ENABLE_EMAIL)")
        return False
    envelope = [e for e in (envelope or []) if e]
    if kind:
        try:
            envelope, _cc, removed = without_client(envelope, [], system or "", kind)
        except Exception as exc:  # noqa: BLE001 -- fail closed: no client filter, no mail
            _set_reason(f"client filter failed: {exc}")
            log.error(f"mail not sent ({kind}): client filter failed: {exc}")
            return False
        if removed:
            gone = {r.lower() for r in removed}
            for header in ("To", "Cc"):
                value = msg.get(header)
                if value is None:
                    continue
                kept = [a.strip() for a in str(value).split(",")
                        if a.strip() and a.strip().lower() not in gone
                        and not any(g in a.lower() for g in gone)]
                del msg[header]
                if kept:
                    msg[header] = ", ".join(kept)
        if not envelope:
            _set_reason("no recipients (client addresses removed)")
            log.warning(f"mail not sent, no recipients left: {msg.get('Subject', '')}")
            return False
    if system and kind and is_critical(kind, severity):
        outage = mail_suspended(smtp_config)
        if outage:
            try:
                from core import alert_ledger
                alert_ledger._whatsapp_copy(system, kind, alert_ledger._norm_key(None),
                                            str(msg.get("Subject", "") or kind),
                                            f"mail account suspended ({outage})")
            except Exception as exc:  # noqa: BLE001
                log.debug(f"whatsapp copy skipped: {exc}")
    try:
        ok, reason = transmit(msg, smtp_config, envelope, kind=kind, severity=severity)
    except Exception as exc:  # noqa: BLE001
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        log.error(f"mail failed: {msg.get('Subject', '')}: {reason}")
        _set_reason(reason)
    return ok


# ------------------------------------------------------------- asynchronous

_q: "queue.Queue" = queue.Queue(maxsize=500)
_worker: dict = {"thread": None}
_worker_lock = threading.Lock()


def _drain() -> None:
    while True:
        args, kwargs = _q.get()
        try:
            send_html_now(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.error(f"background mail failed: {type(exc).__name__}: {exc}")
        finally:
            _q.task_done()


def _ensure_worker() -> None:
    with _worker_lock:
        t = _worker["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_drain, daemon=True, name="ibo-mail-worker")
            t.start()
            _worker["thread"] = t


def deliver(subject: str, html: str, to: list[str], *, system: str = "", kind: str = "",
            cc: list[str] | None = None, text_fallback: str | None = None, severity: str = "",
            key=None, min_interval_s: float = 0, daily_cap: int | None = None,
            smtp_config: dict | None = None) -> bool:
    """
    The non-blocking way to mail: gate decision now, sending in the background.

    Returns True when the mail was accepted (queued), False when the gate held
    it (the reason is recorded in Alert history) or it had no recipients.
    With IBO_MAIL_ASYNC on, the jobs service sends it; otherwise an
    in-process worker thread does.
    """
    from core import alert_ledger
    if mail_async():
        return send_html(subject, html, to, smtp_config or {}, cc, text_fallback, system, kind, key,
                         min_interval_s, daily_cap, severity)
    to, cc, _removed = without_client(to, cc, system, kind)
    if not to:
        log.warning(f"mail not queued, no recipients: {subject}")
        return False
    if not email_enabled():
        log.info(f"mail suppressed (IBO_ENABLE_EMAIL=0): {subject} -> {to}")
        return False
    if system and kind:
        ok, why = alert_ledger.should_send(system, kind, key, min_interval_s, daily_cap,
                                           severity=severity)
        if not ok:
            log.info(f"{system}: {kind} mail held: {why}")
            return False
    if smtp_config is None:
        from core.config_loader import get_smtp_config
        smtp_config = get_smtp_config(_cfg_for(system) if system else None, require_recipients=False)
    html = with_action_footer(html, system, kind, list(to) + list(cc))
    _ensure_worker()
    try:
        _q.put_nowait(((subject, html, to, smtp_config), {
            "cc": cc, "text_fallback": text_fallback, "system": system, "kind": kind, "key": key,
            "severity": severity}))
    except queue.Full:
        log.error(f"mail queue full (500): {subject} dropped")
        if system and kind:
            alert_ledger.record(system, kind, key, severity, subject, list(to) + list(cc),
                                delivered=False, reason="mail queue full")
        return False
    return True


enqueue_mail = deliver


def wait_idle(timeout: float = 10.0) -> bool:
    """Tests: wait until the in-process worker has sent everything queued."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _q.unfinished_tasks == 0:
            return True
        time.sleep(0.02)
    return False


__all__ = ["deliver", "enqueue_mail", "send_html", "send_html_now", "send_message", "transmit",
           "mail_suspended", "suspension", "suspend", "clear_suspension", "email_enabled",
           "mail_async", "last_reason", "classify", "with_action_footer"]

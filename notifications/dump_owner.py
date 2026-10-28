"""
Tell the team that owns a memory-dump spike.

The SAP_MEMORY_DUMP_ATTRIBUTION rule decides ABAP / BASIS / FUNCTIONAL /
TRIAGE and names the program. This module turns that into one mail to the
right mailbox. It is job_owner.py's sibling and keeps the same four guards:

Opt-in. Off unless NOTIFY_DUMP_OWNERS=true.

Team mailboxes from configuration, never guessed:
    DUMP_TEAM_EMAIL_ABAP=abap-team@company.com,lead@company.com
    DUMP_TEAM_EMAIL_FUNCTIONAL=sap-functional@company.com
    DUMP_TEAM_EMAIL_BASIS=...            (defaults to the alert recipients)
A team with no mailbox is logged as unreachable and the mail goes to Basis
only -- that is a configuration gap someone should close, not a silent drop.

Basis always copied. The owner fixes their program; only Basis sees whether
the same memory was also starving three other things.

Once per culprit per day. A Z report that dumps every ten minutes would
otherwise send a mail every sweep, and mails that arrive identically at
3am and 3pm get a filter pointed at them within a week.

TRIAGE goes to Basis only, flagged as unattributed -- someone has to look,
and Basis holds first line.
"""

from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.dump_attribution import parse_header
from utils.logger import get_logger

log = get_logger(__name__)

RULE_ID = "SAP_MEMORY_DUMP_ATTRIBUTION"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _enabled() -> bool:
    return os.getenv("NOTIFY_DUMP_OWNERS", "false").strip().lower() == "true"


def team_addresses(team: str, smtp_config: dict) -> list[str]:
    raw = os.getenv(f"DUMP_TEAM_EMAIL_{team.upper()}", "")
    addresses = [a.strip().strip("<>").strip() for a in raw.split(",") if a.strip()]
    if not addresses and team.upper() == "BASIS":
        addresses = list(smtp_config.get("to_emails") or [])
    return addresses


def _today(key: str) -> str:
    """The SAP server's date for the system the key is about (core/sap_clock.py)."""
    try:
        from core.sap_clock import sap_today
        return sap_today(str(key).split(":", 1)[0])
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def _state_path() -> str:
    return os.path.join(BASE_DIR, "config", "dump_owner_notified.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        from utils.atomic import atomic_write_json
        atomic_write_json(_state_path(), state)
    except OSError as exc:
        log.warning(f"Could not persist dump-owner state: {exc}")


def _already_sent(key: str) -> bool:
    return _load_state().get(key) == _today(key)


def _mark_sent(key: str) -> None:
    state = _load_state()
    state[key] = _today(key)
    _save_state(state)


def verdict_from_incident(incident) -> dict | None:
    """The parsed header line, or None if this is not an attribution incident."""
    rule_id = getattr(incident, "rule_id", None) or (incident.get("rule_id") if isinstance(incident, dict) else "")
    if rule_id != RULE_ID:
        return None
    evidence = getattr(incident, "evidence", None) or (incident.get("evidence") if isinstance(incident, dict) else []) or []
    for line in evidence:
        parsed = parse_header(line)
        if parsed.get("owner_team"):
            parsed["_evidence"] = list(evidence)
            return parsed
    return None


def _body(system: str, v: dict, incident) -> tuple[str, str]:
    team, verdict, culprit = v["owner_team"], v.get("verdict", "?"), v.get("culprit", "-")
    n, progs = v.get("memory_dumps", "?"), v.get("programs", "?")
    title = getattr(incident, "title", None) or (incident.get("title") if isinstance(incident, dict) else "") or "Memory dump spike"
    first_seen = getattr(incident, "first_seen", None)
    when = first_seen.strftime("%Y-%m-%d %H:%M") if hasattr(first_seen, "strftime") else str(first_seen or "")
    incident_id = getattr(incident, "incident_id", None) or (incident.get("incident_id") if isinstance(incident, dict) else "") or ""

    if team == "TRIAGE":
        subject = f"[{system}] {n} memory dumps -- UNATTRIBUTED, needs a look"
    else:
        subject = f"[{system}] {n} memory dumps -> {team}: {culprit}"

    # Everything below goes into HTML and comes from SAP (program names,
    # evidence lines): escaped. The subject above is plain text.
    import html as _h
    e = lambda x: _h.escape(str(x if x is not None else ""))  # noqa: E731
    team, verdict, culprit, n, progs = e(team), e(verdict), e(culprit), e(n), e(progs)
    title, when, incident_id, system = e(title), e(when), e(incident_id), e(system)
    v = dict(v, _evidence=[e(line) for line in v["_evidence"]])

    rows = "".join(f"<li>{line.strip()}</li>" for line in v["_evidence"][1:] if line.strip())
    verdict_text = {
        "single_hog": f"<b>{culprit}</b> is customer code and took most of the memory dumps. This is a code fix.",
        "hog_with_collateral": (f"<b>{culprit}</b> is customer code and was holding the memory. The other dumps in this "
                                f"window are downstream of it -- one ticket, not several. Basis is copied so nobody "
                                f"chases the collateral."),
        "system_starvation": (f"{n} memory dumps over {progs} unrelated programs and no customer program holding memory. "
                              f"The server is short, not one report. Check ST02 for the window and the memory profile."),
        "standard_code": (f"<b>{culprit}</b> is SAP standard code. Data volume, customising or an SAP Note -- "
                          f"not an ABAP defect and not a Basis parameter."),
        "unclear": "The evidence did not point to one owner. The figures seen are listed below.",
    }.get(verdict, verdict)

    html = f"""
<html><body style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">
<h3 style="margin:0 0 6px">{title}</h3>
<p style="margin:0 0 12px;color:#666">{system} · first seen {when} · incident {incident_id} · owner <b>{team}</b> · verdict {verdict}</p>
<p>{verdict_text}</p>
<p style="margin:14px 0 4px"><b>What the monitor saw</b></p>
<ul style="margin:0 0 12px 18px;padding:0;line-height:1.5">{rows}</ul>
<p style="color:#666;font-size:12px">Deterministic attribution from InfraBeatOps rule {RULE_ID}. Same inputs, same verdict;
every figure relied on is listed above. Wrong team? Reply to this mail so the ladder can be corrected.</p>
</body></html>"""
    return subject, html


def notify_dump_owner(system: str, incidents, smtp_config: dict, basis_recipients=None) -> dict:
    """Mail the owning team for each attribution incident. Never raises."""
    if not _enabled():
        return {"sent": 0, "reason": "NOTIFY_DUMP_OWNERS is not enabled"}

    # Basis: the given list, DUMP_TEAM_EMAIL_BASIS, the report list without the
    # client, else the Basis consultants. Never the client (internal mail).
    from core import recipients as _rcp
    from notifications import mailer
    _cfg = mailer._cfg_for(system)
    basis = list(basis_recipients or team_addresses("BASIS", {}) or [])
    if not basis:
        basis = (_rcp.strip_client(list(smtp_config.get("to_emails") or []), _cfg)[0]
                 or _rcp.basis_internal(_cfg))
    sent, skipped, unreachable = 0, [], []

    for incident in incidents or []:
        v = verdict_from_incident(incident)
        if not v:
            continue
        team = v["owner_team"].upper()
        culprit = v.get("culprit", "-")

        if team in ("TRIAGE", "BASIS"):
            to = basis
        else:
            to = team_addresses(team, smtp_config)
            if not to:
                unreachable.append(f"{team} for {culprit}: no DUMP_TEAM_EMAIL_{team} configured -- sent to Basis only")
                to = basis
        if not to:
            skipped.append(f"{culprit}: no recipients at all")
            continue

        key = f"{system}:{team}:{culprit}"
        if _already_sent(key):
            skipped.append(f"{culprit}: {team} already notified today")
            continue

        subject, html = _body(system, v, incident)
        cc = [a for a in basis if a not in to]
        # Internal mail: client addresses out, through the delivery gate and
        # the shared transport (20 s timeout, no retry on a 5xx).
        to, cc, _removed = mailer.without_client(to, cc, system, "dump_owner")
        if not to:
            skipped.append(f"{culprit}: no internal recipients")
            continue
        from core import owner_mail
        _basis_low = {x.lower() for x in basis}
        if owner_mail.approval_on() and any(x.lower() not in _basis_low for x in to):
            # Basis approves first (core/owner_mail.py); nothing goes to the team yet.
            rid = owner_mail.request(system, "dump_owner", key, to, cc, subject, html, subject,
                                     f"dumps from {culprit} ({team})", basis)
            _mark_sent(key)
            if rid:
                skipped.append(f"{culprit}: waiting for Basis approval (request #{rid})")
            continue
        from core import alert_ledger
        ok_gate, why = alert_ledger.should_send(system, "dump_owner", key, 0, None, severity="WARNING")
        if not ok_gate:
            skipped.append(f"{culprit}: held ({why})")
            continue
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = smtp_config["from_email"]
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message.attach(MIMEText(f"{subject}. This message needs an HTML mail client.", "plain"))
        message.attach(MIMEText(html, "html"))

        ok = mailer.send_message(message, smtp_config, to + cc, system=system, kind="dump_owner")
        alert_ledger.record(system, "dump_owner", key, "WARNING", subject, to + cc, delivered=ok,
                            reason="" if ok else mailer.last_reason())
        if ok:
            _mark_sent(key)
            sent += 1
            log.info(f"Dump owner notified: {team} <{', '.join(to)}> about {culprit} on {system}")
        else:
            log.warning(f"Could not notify {team} about {culprit}: {mailer.last_reason()}")

    if unreachable:
        log.warning(f"{system}: {len(unreachable)} team mailbox(es) unset -- {'; '.join(unreachable[:3])}")
    return {"sent": sent, "skipped": skipped, "unreachable": unreachable}

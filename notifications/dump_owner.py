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
    addresses = [a.strip() for a in raw.split(",") if a.strip()]
    if not addresses and team.upper() == "BASIS":
        addresses = list(smtp_config.get("to_emails") or [])
    return addresses


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
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
    except OSError as exc:
        log.warning(f"Could not persist dump-owner state: {exc}")


def _already_sent(key: str) -> bool:
    return _load_state().get(key) == datetime.now().strftime("%Y-%m-%d")


def _mark_sent(key: str) -> None:
    state = _load_state()
    state[key] = datetime.now().strftime("%Y-%m-%d")
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

    basis = list(basis_recipients or smtp_config.get("to_emails") or [])
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
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = smtp_config["from_email"]
        message["To"] = ", ".join(to)
        cc = [a for a in basis if a not in to]
        if cc:
            message["Cc"] = ", ".join(cc)
        message.attach(MIMEText(f"{subject}. This message needs an HTML mail client.", "plain"))
        message.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=60) as server:
                server.ehlo(); server.starttls(); server.ehlo()
                server.login(smtp_config["username"], smtp_config["password"])
                server.sendmail(smtp_config["from_email"], to + cc, message.as_string())
            _mark_sent(key)
            sent += 1
            log.info(f"Dump owner notified: {team} <{', '.join(to)}> about {culprit} on {system}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Could not notify {team} about {culprit}: {exc}")

    if unreachable:
        log.warning(f"{system}: {len(unreachable)} team mailbox(es) unset -- {'; '.join(unreachable[:3])}")
    return {"sent": sent, "skipped": skipped, "unreachable": unreachable}

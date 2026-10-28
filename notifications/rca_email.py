"""
Send the Performance RCA report to the Basis consultant.

Same SMTP configuration as the critical alert (core.config_loader
get_smtp_config), so a system with its own smtp_* settings uses them and
everyone else inherits the global .env values. The PDF is attached; the body
is the headline and culprits so the consultant can act from the phone
before opening the attachment.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from utils.logger import get_logger

log = get_logger(__name__, "email")


def _esc(s) -> str:
    import html
    return html.escape(str(s if s is not None else ""))


def build_rca_html(analysis: dict, trigger_reason: str, system: str = "") -> str:
    """The RCA mail in plain words, with the user / lock / program / server names highlighted."""
    from notifications import mail_style as ms, plain_rca as pr
    sev = str(analysis.get("severity", "WARNING")).upper()
    culprits = [c for c in (analysis.get("culprits") or []) if isinstance(c, dict)]
    names = pr.names_of(culprits)
    what_html, _short = pr.trigger(trigger_reason)
    who = system or "The system"
    title = (f"{who} was slow -- here is what IBOPS found" if _short.startswith("slow")
             else f"{who}: performance check")
    blocks = []
    root = str(analysis.get("root_cause") or "").strip()
    if root:
        blocks.append(ms.section("Most likely cause", ms.callout(ms.highlight_names(root, names), sev)))
    if culprits:
        rows = []
        for c in culprits[:6]:
            p = pr.culprit(c)
            rows.append(
                "<tr><td style='padding:10px 10px 10px 0;vertical-align:top;width:92px'>"
                f"{ms.pill(p['kind'], p['tone'])}</td>"
                f"<td style='padding:10px 0;border-bottom:1px solid #e2e8f0;font:14px/1.5 {ms.FONT};color:#0f172a'>"
                f"<div style='margin:0 0 3px'>{p['who']}</div><div>{p['what']}</div>"
                + (f"<div style='color:#64748b;font-size:13px;margin-top:3px'>Why it matters: {ms.esc(p['why'])}</div>"
                   if p["why"] else "")
                + "</td></tr>")
        blocks.append(ms.section("Who and what was involved",
                                 "<table role='presentation' style='border-collapse:collapse;width:100%'>"
                                 + "".join(rows) + "</table>"))
    acts = analysis.get("actions_now") or []
    if acts:
        blocks.append(ms.section("What to do now", ms.steps([pr.action(a, names) for a in acts[:6]])))
    foot = (pr.confidence(analysis) + " The PDF attached has every SAP screen this is based on. "
            "Internal mail -- never sent to the client.")
    eyebrow = f"PERFORMANCE ANALYSIS &nbsp;{ms.badge(sev)} &nbsp;{ms.esc(ms.ist())}"
    return ms.page(sev, eyebrow, title, what_html, blocks, foot)


def plain_subject(prefix: str, system: str, analysis: dict, trigger_reason: str) -> str:
    """'[InfraBeatOps RCA] CAR PRD -- WARNING: slow screens (2.9 s), look at CPIUSER'."""
    from notifications import plain_rca as pr
    sev = str(analysis.get("severity", "?")).upper()
    _html, short = pr.trigger(trigger_reason)
    culprits = [c for c in (analysis.get("culprits") or []) if isinstance(c, dict)]
    lead = ""
    root = str(analysis.get("root_cause") or "")
    if "No single user's work explains it" in root or root.startswith("Screens on"):
        found = [w for k, w in (("GUI time", "network / PC time"), ("roll-in", "memory"),
                                ("the database", "database"), ("(CPU)", "CPU"),
                                ("free work process", "no free work processes"), ("locks", "lock waits"))
                 if k in root.split("No single")[0]]
        if found:
            lead = "mostly " + " and ".join(found[:2])
    for c in ([] if lead else culprits):
        n = str(c.get("name") or "").split(" (")[0].split(" on ")[0].strip()
        if n and str(c.get("type")) in ("user", "lock", "report", "job"):
            lead = f"look at {n}"
            break
    words = ", ".join(x for x in (short, lead) if x) or str(analysis.get("headline") or "")[:90]
    return f"{prefix} {system} -- {sev}: {words}"


def send_rca_report(smtp_config: dict, recipients: list[str], subject: str,
                    pdf_path: str, analysis: dict, trigger_reason: str,
                    system_cfg: dict | None = None) -> bool:
    """The RCA mail: internal (never the client), through the delivery gate and the shared transport."""
    from notifications import mailer
    from core import alert_ledger
    system = str((system_cfg or {}).get("name") or "")
    recipients, _cc, removed = mailer.without_client(recipients, [], system, "rca")
    if not recipients:
        log.warning(f"RCA email for {system}: no internal recipients"
                    + (f" (client addresses removed: {removed})" if removed else ""))
        return False
    if system:
        ok, why = alert_ledger.should_send(system, "rca", subject[:120], 0, 0,
                                           severity=str(analysis.get("severity", "")))
        if not ok:
            log.info(f"RCA email for {system} held: {why}")
            return False
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(build_rca_html(analysis, trigger_reason, system), "html"))
    msg.attach(alt)
    try:
        with open(pdf_path, "rb") as fh:
            att = MIMEApplication(fh.read(), _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename=os.path.basename(pdf_path))
        msg.attach(att)
    except OSError as e:
        log.error(f"RCA email: could not attach {pdf_path}: {e}")
        if system:
            alert_ledger.record(system, "rca", subject[:120], "", subject, recipients,
                                delivered=False, reason=f"attachment missing: {e}")
        return False
    ok = mailer.send_message(msg, smtp_config, recipients, system=system, kind="rca",
                             severity=str(analysis.get("severity", "")))
    if system:
        alert_ledger.record(system, "rca", subject[:120], str(analysis.get("severity", "")), subject,
                            recipients, delivered=ok, reason="" if ok else mailer.last_reason())
    if ok:
        log.info(f"RCA email sent to {recipients}: {subject}")
    else:
        log.error(f"RCA email failed: {mailer.last_reason()}")
    return ok

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
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_rca_html(analysis: dict, trigger_reason: str) -> str:
    sev = str(analysis.get("severity", "WARNING")).upper()
    colour = {"CRITICAL": "#C62828", "WARNING": "#EF6C00"}.get(sev, "#2E7D32")
    parts = [
        f"<h2 style='margin:0 0 6px 0'>Performance RCA — <span style='color:{colour}'>{sev}</span></h2>",
        f"<p><b>{_esc(analysis.get('headline', ''))}</b></p>",
        f"<p style='color:#555'>Trigger: {_esc(trigger_reason)}</p>",
    ]
    culprits = analysis.get("culprits") or []
    if culprits:
        parts.append("<h3>Who / what</h3><ul>")
        for c in culprits[:6]:
            parts.append(f"<li><b>{_esc(c.get('type'))}</b> {_esc(c.get('name'))} — "
                         f"{_esc(c.get('evidence'))}</li>")
        parts.append("</ul>")
    parts.append(f"<h3>Root cause</h3><p>{_esc(analysis.get('root_cause', ''))}</p>")
    acts = analysis.get("actions_now") or []
    if acts:
        parts.append("<h3>Actions now</h3><ol>")
        for a in acts[:6]:
            if isinstance(a, dict):
                parts.append(f"<li>{_esc(a.get('step'))} <i>({_esc(a.get('tcode', ''))})</i></li>")
            else:
                parts.append(f"<li>{_esc(a)}</li>")
        parts.append("</ol>")
    parts.append(f"<p style='color:#777;font-size:12px'>Confidence {_esc(analysis.get('confidence', '?'))} · "
                 f"{_esc(analysis.get('confidence_reason', ''))}. Full report with screens attached.</p>")
    return "\n".join(parts)


def send_rca_report(smtp_config: dict, recipients: list[str], subject: str,
                    pdf_path: str, analysis: dict, trigger_reason: str) -> bool:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(build_rca_html(analysis, trigger_reason), "html"))
    msg.attach(alt)
    try:
        with open(pdf_path, "rb") as fh:
            att = MIMEApplication(fh.read(), _subtype="pdf")
        att.add_header("Content-Disposition", "attachment", filename=os.path.basename(pdf_path))
        msg.attach(att)
    except OSError as e:
        log.error(f"RCA email: could not attach {pdf_path}: {e}")
        return False
    try:
        with smtplib.SMTP(smtp_config["host"], int(smtp_config["port"]), timeout=20) as server:
            if smtp_config.get("use_tls", True):
                server.starttls()
            if smtp_config.get("username"):
                server.login(smtp_config["username"], smtp_config["password"])
            server.sendmail(smtp_config["from_email"], recipients, msg.as_string())
        log.info(f"RCA email sent to {recipients}: {subject}")
        return True
    except Exception as e:  # noqa: BLE001
        log.error(f"RCA email failed: {type(e).__name__}: {e}")
        return False

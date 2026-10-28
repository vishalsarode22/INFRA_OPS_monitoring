"""
Sends the final monitoring report email with PDF and Excel attached, and
the short internal notices around a sweep (T-code capture failed, failure
notice, diagnostic alert).

Every mail here goes through the shared transport (notifications/mailer.py:
20 s timeout, no retry on a permanent 5xx, mail-outage suspension). The
internal notices also pass the delivery gate (core/alert_ledger.py) and never
reach the client. Values from SAP or the AI model are HTML-escaped.
"""

import html as _html
import os
import smtplib  # noqa: F401 -- kept: tests patch smtplib.SMTP through this module
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from core.models import MonitoringResult
from utils.logger import get_logger

log = get_logger(__name__, "email")

STATUS_COLORS = {"NORMAL": "#2e7d32", "WARNING": "#e65100", "CRITICAL": "#c62828", "UNKNOWN": "#616161"}


def _e(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _build_report_html(result: MonitoringResult) -> str:
    status_val = result.overall_status.value
    status_color = STATUS_COLORS.get(status_val, "#616161")
    critical_count = len(result.critical_metrics())
    warning_count = len([m for m in result.metrics if m.status.value == "WARNING"])

    ai = result.ai_analysis
    ai_block = ""
    if ai and ai.raw_response:
        ai_block = f"""
        <div style="background:#f8f9fb;border-radius:6px;padding:16px 20px;margin-top:20px;">
          <div style="font-size:13px;color:#888;margin-bottom:6px;">ANALYSIS</div>
          <div style="font-size:14px;color:#222;margin-bottom:4px;"><b>Severity:</b> {_e(ai.severity)}</div>
          <div style="font-size:14px;color:#222;margin-bottom:4px;"><b>Root Cause:</b> {_e(ai.likely_root_cause)}</div>
          <div style="font-size:14px;color:#222;"><b>Confidence:</b> {_e(ai.confidence)}</div>
        </div>"""

    return f"""\
<html>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr>
          <td style="background:#263238;padding:24px 28px;">
            <span style="color:#ffffff;font-size:20px;font-weight:700;">InfraBeatOps Report</span><br>
            <span style="color:#b0bec5;font-size:13px;">{_e(result.system)} / Client {_e(result.client)}</span>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px;">
            <div style="display:inline-block;background:{status_color}1a;color:{status_color};font-weight:700;font-size:15px;padding:8px 16px;border-radius:20px;margin-bottom:16px;">
              {_e(status_val)}
            </div>
            <table cellpadding="0" cellspacing="0" style="width:100%;margin:12px 0 4px 0;">
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Cycle time</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{result.cycle_timestamp.strftime('%Y-%m-%d %H:%M:%S')}</td>
              </tr>
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Metrics evaluated</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{len(result.metrics)}</td>
              </tr>
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Critical / Warning</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{critical_count} / {warning_count}</td>
              </tr>
            </table>
            {ai_block}
            <div style="margin-top:20px;font-size:13px;color:#666;">
              Full metric breakdown, recommendations, and T-code evidence screenshots are
              attached as a PDF report. Historical monitoring data is attached as an Excel file.
            </div>
          </td>
        </tr>
        <tr>
          <td style="background:#fafafa;padding:16px 28px;text-align:center;">
            <span style="color:#aaa;font-size:11px;">InfraBeatOps -- Automated Report</span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def _action_footer_text(system_name: str, kind: str, smtp_config: dict) -> str:
    """Snooze / acknowledge links for the plain-text failure mails (core/action_links.py)."""
    try:
        from core.action_links import footer_text, internal_strict
        if internal_strict(smtp_config.get("to_emails") or []):
            return footer_text(system_name, kind)
    except Exception:
        pass
    return ""


def _internal_to(system_name: str, smtp_config: dict) -> list[str]:
    """The receivers of an internal notice: the given list without the client, else Basis."""
    from core import recipients
    from notifications import mailer
    cfg = mailer._cfg_for(system_name)
    to, _removed = recipients.strip_client(list(smtp_config.get("to_emails") or []), cfg)
    return to or recipients.basis_internal(cfg)


def _text_mail(system_name: str, kind: str, subject: str, body: str, smtp_config: dict,
               key=None, min_interval_s: float = 0, daily_cap=None, severity: str = "WARNING") -> bool:
    """A plain-text internal notice: gate, client removed, sent in the background."""
    from notifications import mailer
    to = _internal_to(system_name, smtp_config)
    if not to:
        log.warning(f"{system_name}: {kind} notice not sent -- no internal recipients")
        return False
    html_body = ("<html><body><pre style='font:13px Consolas,monospace;white-space:pre-wrap'>"
                 f"{_e(body)}</pre></body></html>")
    return bool(mailer.deliver(subject, html_body, to, system=system_name, kind=kind,
                               text_fallback=body, severity=severity, key=key,
                               min_interval_s=min_interval_s, daily_cap=daily_cap,
                               smtp_config=smtp_config if smtp_config.get("host") else None))


def send_tcode_failure_alert(system_name: str, failed_tcodes: list, smtp_config: dict, max_retries: int = 2) -> bool:
    """
    Sends a short internal notice when the pipeline completed but one or
    more T-codes failed to capture even after their own retries. Same
    T-codes failing sweep after sweep is one problem: repeat held 6 h, a
    changed set goes out at once (core/alert_ledger.py). `max_retries` is
    kept for callers; the transport decides retries (no retry on a 5xx).
    """
    tcode_list = "\n".join(f"  - {t}" for t in failed_tcodes)
    subject = f"[WARNING] InfraBeatOps -- {system_name}: {len(failed_tcodes)} T-code(s) could not be captured"
    body = (
        f"The monitoring run for {system_name} finished, "
        f"but the following T-code(s) failed to capture even after retrying:\n\n"
        f"{tcode_list}\n\n"
        f"These show as \"failed\" in this cycle's report; while checks are missing the "
        f"monitoring sheet is not sent to the client. Check logs\\application.log "
        f"for the specific errors, or logs\\dashboard.log if this ran via the scheduler."
    )
    body += _action_footer_text(system_name, "tcode_failure", smtp_config)
    return _text_mail(system_name, "tcode_failure", subject, body, smtp_config,
                      key=sorted(str(t) for t in failed_tcodes), min_interval_s=6 * 3600)


def send_failure_alert(system_name: str, error_message: str, attempt: int,
                       max_attempts: int, smtp_config: dict, max_retries: int = 2) -> bool:
    """
    The "pipeline failed (attempt n/m)" / "no monitoring data" notice.

    The owner does not want these mails (Oct 2026): kind "sweep_failure" is
    switched off by default in the delivery gate (IBO_DISABLED_MAIL_KINDS),
    which records each one in Alert history as suppressed-policy. Remove
    sweep_failure from that setting to bring them back (capped 6 a day).
    """
    subject = f"[ALERT] InfraBeatOps -- {system_name} pipeline failed (attempt {attempt}/{max_attempts})"
    body = (
        f"The monitoring pipeline for system {system_name} failed on attempt "
        f"{attempt} of {max_attempts}.\n\n"
        f"Error:\n{error_message}\n\n"
        + ("The system will retry automatically.\n"
           if attempt < max_attempts else
           "All retry attempts have been exhausted -- this system will be "
           "skipped for this monitoring cycle. Manual investigation needed.\n")
        + "\nCheck logs\\application.log for the full traceback."
    )
    body += _action_footer_text(system_name, "sweep_failure", smtp_config)
    return _text_mail(system_name, "sweep_failure", subject, body, smtp_config,
                      key=str(attempt), daily_cap=6, severity="ALERT")


def send_final_report(result: MonitoringResult, smtp_config: dict,
                      pdf_path: str, excel_path: str, max_retries: int = 2, note_html: str = "") -> bool:
    """The scheduled report (the one mail the client gets). `max_retries` is ignored: the transport decides.
    `note_html` goes at the top (report_gate.partial_note: checks that could not be captured)."""
    from notifications import mailer
    subject = f"InfraBeatOps Report - {result.system}"
    html_body = _build_report_html(result)
    if note_html:
        import re as _re
        m = _re.search(r"<body[^>]*>", html_body, _re.I)
        html_body = (html_body[:m.end()] + note_html + html_body[m.end():]) if m else note_html + html_body

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    to = list(smtp_config["to_emails"])
    # Cc (the profile's email_cc, or the system's CC list) was configured but
    # never used: the report only ever went to To.
    cc = [a for a in dict.fromkeys(smtp_config.get("cc_emails") or []) if a and a not in to]
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for file_path in [pdf_path, excel_path]:
        if not file_path or not os.path.exists(file_path):
            log.warning(f"Attachment not found, skipping: {file_path}")
            continue
        try:
            with open(file_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(file_path))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(file_path)}"'
            msg.attach(part)
        except Exception as e:
            log.error(f"Failed to attach {file_path}: {e}")

    ok = mailer.send_message(msg, smtp_config, to + cc,
                             system=result.system, kind="report")
    if ok:
        log.info(f"Final report email sent to {to}" + (f" cc {cc}" if cc else ""))
    else:
        log.error(f"Failed to send final report email: {mailer.last_reason()}")
    return ok


def send_diagnostic_alert(result, smtp_config: dict, incident=None,
                          ai_analysis=None, max_retries: int = 2) -> bool:
    """
    Sends the full RCA alert to the Basis consultant (internal: never the client).

    Use instead of send_final_report when an incident was correlated or the
    cycle is CRITICAL: it carries the culprit, the evidence behind that
    attribution, the remediation steps, and everything that could NOT be read.
    """
    from notifications import mailer
    from notifications.diagnostic_alert import build_diagnostic_alert

    subject, html_body = build_diagnostic_alert(result, incident, ai_analysis)
    to = _internal_to(result.system, smtp_config)
    if not to:
        log.warning(f"{result.system}: diagnostic alert not sent -- no internal recipients")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(
        f"System {result.system} is {result.overall_status}. "
        "This alert requires an HTML-capable mail client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    ok = mailer.send_message(msg, smtp_config, to, system=result.system, kind="diagnostic",
                             severity=getattr(result.overall_status, "value", ""))
    if ok:
        log.info(f"Diagnostic alert sent to {to}")
    else:
        log.error(f"Failed to send diagnostic alert: {mailer.last_reason()}")
    return ok

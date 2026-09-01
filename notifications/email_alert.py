"""
Sends an immediate CRITICAL alert email as soon as a monitoring cycle
detects a critical condition. Does NOT wait for the full PDF report --
that comes later via notifications/email_report.py.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from core.models import MonitoringResult
from utils.logger import get_logger

log = get_logger(__name__, "email")


def _build_alert_html(result: MonitoringResult) -> str:
    critical_metrics = result.critical_metrics()
    rows = "".join(
        f"""<tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">{m.name}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#c62828;font-weight:600;">{m.display_value}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#555;">{m.detail}</td>
        </tr>"""
        for m in critical_metrics
    )

    return f"""\
<html>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        <tr>
          <td style="background:#c62828;padding:20px 28px;">
            <span style="color:#ffffff;font-size:20px;font-weight:700;">🚨 SAP {result.system} CRITICAL ALERT</span>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px;">
            <table cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:20px;">
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">System</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{result.system}</td>
              </tr>
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Client</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{result.client}</td>
              </tr>
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Time</td>
                <td style="color:#222;font-size:13px;font-weight:600;padding:4px 0;">{result.cycle_timestamp.strftime('%Y-%m-%d %H:%M:%S')}</td>
              </tr>
              <tr>
                <td style="color:#888;font-size:13px;padding:4px 0;">Severity</td>
                <td style="color:#c62828;font-size:13px;font-weight:700;padding:4px 0;">CRITICAL</td>
              </tr>
            </table>

            <div style="font-size:14px;color:#333;font-weight:600;margin-bottom:8px;">Issue(s) detected:</div>
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:4px;overflow:hidden;margin-bottom:20px;">
              <tr style="background:#fafafa;">
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#888;">Metric</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#888;">Value</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#888;">Detail</th>
              </tr>
              {rows}
            </table>

            <div style="background:#fff8e1;border-left:4px solid #f9a825;padding:12px 16px;border-radius:4px;font-size:13px;color:#666;">
               Analysis: <em>Pending...</em> A detailed report will follow.
            </div>
          </td>
        </tr>
        <tr>
          <td style="background:#fafafa;padding:16px 28px;text-align:center;">
            <span style="color:#aaa;font-size:11px;">InfraBeatOps -- Automated Alert</span>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def send_critical_alert(result: MonitoringResult, smtp_config: dict) -> bool:
    from core.models import Status

    if result.overall_status != Status.CRITICAL:
        log.debug("Overall status is not CRITICAL -- skipping immediate alert.")
        return False

    subject = f"🚨 SAP {result.system} CRITICAL ALERT"
    html_body = _build_alert_html(result)

    # CC recipients must be added to BOTH the header and the envelope.
    # Setting only the header shows the address in the mail client while the
    # server never delivers to it -- the worst kind of failure, because it
    # looks like it worked.
    cc_emails = smtp_config.get("cc_emails") or []
    recipients = list(smtp_config["to_emails"]) + list(cc_emails)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=15) as server:
            server.starttls()
            server.login(smtp_config["username"], smtp_config["password"])
            server.sendmail(smtp_config["from_email"], recipients, msg.as_string())
        log.info(f"Critical alert email sent to {recipients}")
        return True
    except Exception as e:
        log.error(f"Failed to send critical alert email: {e}")
        return False
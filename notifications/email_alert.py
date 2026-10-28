"""
Sends an immediate CRITICAL alert email as soon as a monitoring cycle
detects a critical condition. Does NOT wait for the full PDF report --
that comes later via notifications/email_report.py.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from core.models import MonitoringResult
from utils.logger import get_logger

log = get_logger(__name__, "email")


def _e(value) -> str:
    import html
    return html.escape(str(value if value is not None else ""))


class _Esc:
    """Read-only view of a result whose text fields come out HTML-escaped."""
    def __init__(self, result):
        self.system, self.client = _e(result.system), _e(result.client)
        self.cycle_timestamp = result.cycle_timestamp


def _build_alert_html(result: MonitoringResult) -> str:
    critical_metrics = result.critical_metrics()
    rows = "".join(
        f"""<tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;">{_e(m.name)}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#c62828;font-weight:600;">{_e(m.display_value)}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#555;">{_e(m.detail)}</td>
        </tr>"""
        for m in critical_metrics
    )
    result = _Esc(result)   # system / client are SAP-side text: escaped below

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
    from core.alert_engine import legacy_value_mails
    if not legacy_value_mails():
        log.info(f"{result.system}: combined CRITICAL mail not sent -- values alert one by one as chosen "
                 f"on the Thresholds page (IBO_LEGACY_VALUE_MAILS=1 brings it back).")
        return False

    html_body = _build_alert_html(result)

    # Two-tier escalation (core/escalation.py): the first breach of a metric
    # goes to the Basis consultants; the same metric still critical on the
    # next sweep adds the Basis manager and turns the subject red.
    from core.escalation import assess
    critical_names = [m.name for m in result.metrics
                      if getattr(m.status, "value", m.status) == "CRITICAL"]
    tier = assess(result.system, critical_names)
    if tier.escalate:
        subject = (f"🔴 SAP {result.system} CRITICAL — REPEATED "
                   f"({', '.join(tier.repeated)[:80]}) — escalated to manager")
    else:
        subject = f"🟡 SAP {result.system} CRITICAL ALERT (first occurrence)"

    # Repeat suppression (core/alert_ledger.py). The key includes the tier,
    # so the first RED escalation after a YELLOW always goes out; an
    # identical alert (same tier, same metrics) is held for
    # IBO_CRITICAL_ALERT_MIN_MINUTES (default 60). Escalation state above is
    # updated every sweep regardless, so holding a mail never resets a tier.
    from core import alert_ledger
    try:
        _interval = int(os.getenv("IBO_CRITICAL_ALERT_MIN_MINUTES", "60") or 60) * 60
    except ValueError:
        _interval = 3600
    _key = ["RED" if tier.escalate else "YELLOW"] + sorted(critical_names)
    _ok, _why = alert_ledger.should_send(result.system, "critical", _key, min_interval_s=_interval,
                                         severity="CRITICAL")
    if not _ok:
        log.info(f"{result.system}: critical alert held: {_why}")
        return False

    # CC recipients must be added to BOTH the header and the envelope.
    # Setting only the header shows the address in the mail client while the
    # server never delivers to it -- the worst kind of failure, because it
    # looks like it worked.
    cc_emails = list(smtp_config.get("cc_emails") or [])
    manager_emails = list(smtp_config.get("manager_emails") or [])
    if tier.escalate and manager_emails:
        cc_emails += [m for m in manager_emails if m not in cc_emails]
    elif tier.escalate:
        log.warning(f"{result.system}: repeated breach of {tier.repeated} but no "
                    "manager address configured (ALERT_MANAGER_EMAILS / "
                    "manager_receivers) -- consultants only.")
    # Whoever is on call (config/oncall.yaml) is copied on every critical
    # alert, so the person on shift sees it even if the system's own list
    # predates the rotation. IBO_ONCALL_CRITICAL=0 turns this off.
    if os.getenv("IBO_ONCALL_CRITICAL", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from core import oncall
            have = {e.lower() for e in list(smtp_config["to_emails"]) + cc_emails}
            cc_emails += [e for e in oncall.basis_emails() if e.lower() not in have]
        except Exception as _exc:  # noqa: BLE001
            log.debug(f"{result.system}: on-call copy skipped: {_exc}")
    recipients = list(smtp_config["to_emails"]) + list(cc_emails)

    # Snooze / acknowledge buttons, runbook link and "last time this was
    # fixed by ..." -- only when every recipient is internal (the critical
    # alert can go to a customer address; see core/action_links.py).
    try:
        from core.action_links import internal_strict
        if internal_strict(recipients):
            from notifications.mailer import with_action_footer
            html_body = with_action_footer(html_body, result.system, "critical")
    except Exception as _exc:  # noqa: BLE001
        log.debug(f"{result.system}: action footer skipped: {_exc}")

    # Internal alert: the client's addresses are taken out (core/recipients.py).
    from notifications import mailer
    to_emails, cc_emails, _removed = mailer.without_client(list(smtp_config["to_emails"]), cc_emails,
                                                           result.system, "critical")
    recipients = to_emails + cc_emails
    if not to_emails:
        alert_ledger.record(result.system, "critical", _key, "CRITICAL", subject, [],
                            delivered=False, reason="no internal recipients (client addresses removed)")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(to_emails)
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)
    msg.attach(MIMEText(html_body, "html"))

    ok = mailer.send_message(msg, smtp_config, recipients, system=result.system, kind="critical",
                             severity="CRITICAL")
    if ok:
        log.info(f"Critical alert email sent to {recipients}")
        alert_ledger.record(result.system, "critical", _key, "CRITICAL", subject, recipients)
        return True
    log.error(f"Failed to send critical alert email: {mailer.last_reason()}")
    alert_ledger.record(result.system, "critical", _key, "CRITICAL", subject,
                        recipients, delivered=False, reason=mailer.last_reason()[:200])
    return False
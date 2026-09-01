"""
Sends the final monitoring report email with PDF and Excel attached.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import os

from core.models import MonitoringResult
from utils.logger import get_logger

log = get_logger(__name__, "email")

STATUS_COLORS = {"NORMAL": "#2e7d32", "WARNING": "#e65100", "CRITICAL": "#c62828", "UNKNOWN": "#616161"}


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
          <div style="font-size:14px;color:#222;margin-bottom:4px;"><b>Severity:</b> {ai.severity}</div>
          <div style="font-size:14px;color:#222;margin-bottom:4px;"><b>Root Cause:</b> {ai.likely_root_cause}</div>
          <div style="font-size:14px;color:#222;"><b>Confidence:</b> {ai.confidence}</div>
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
            <span style="color:#b0bec5;font-size:13px;">{result.system} / Client {result.client}</span>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px;">
            <div style="display:inline-block;background:{status_color}1a;color:{status_color};font-weight:700;font-size:15px;padding:8px 16px;border-radius:20px;margin-bottom:16px;">
              {status_val}
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


def send_tcode_failure_alert(system_name: str, failed_tcodes: list, smtp_config: dict, max_retries: int = 2) -> bool:
    """
    Sends a short alert when the pipeline completed successfully overall,
    but one or more individual T-codes failed to capture even after
    their internal retries (see collect_tcode_evidence()'s per-T-code
    retry loop). This is deliberately separate from send_failure_alert()
    -- a few failed T-codes inside an otherwise-successful run is a much
    smaller problem than the whole pipeline crashing, but it still
    deserves its own notification rather than being silently buried as
    a "failed" entry the person would only notice by opening the PDF.
    """
    tcode_list = "\n".join(f"  - {t}" for t in failed_tcodes)
    subject = f"[WARNING] InfraBeatOps -- {system_name}: {len(failed_tcodes)} T-code(s) could not be captured"
    body = (
        f"The monitoring run for {system_name} completed and a report was sent, "
        f"but the following T-code(s) failed to capture even after retrying:\n\n"
        f"{tcode_list}\n\n"
        f"These show as \"failed\" in this cycle's report. Check logs\\application.log "
        f"for the specific errors, or logs\\dashboard.log if this ran via the scheduler."
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    msg.attach(MIMEText(body, "plain"))

    for attempt in range(1, max_retries + 1):
        try:
            with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_config["username"], smtp_config["password"])
                server.sendmail(smtp_config["from_email"], smtp_config["to_emails"], msg.as_string())
            log.info(f"T-code failure alert sent for {system_name} ({len(failed_tcodes)} failed).")
            return True
        except Exception as e:
            log.warning(f"T-code failure alert attempt {attempt}/{max_retries} failed to send: {e}")
            if attempt == max_retries:
                log.error(f"Could not send T-code failure alert for {system_name}: {e}")
                return False
            import time
            time.sleep(3)
    return False


def send_failure_alert(system_name: str, error_message: str, attempt: int,
                        max_attempts: int, smtp_config: dict, max_retries: int = 2) -> bool:
    """
    Sends an immediate, lightweight plain-text alert when a pipeline run
    fails badly enough that no MonitoringResult was ever produced (e.g.
    SAP Logon crashed, login never completed, an unhandled exception hit
    mid-run). This is separate from send_final_report(), which needs a
    completed MonitoringResult to build its HTML report around -- a hard
    failure has no result to report on, just an error to flag.

    Sent once per exhausted retry attempt (i.e. after each failed try,
    not just the final one), so a persistent problem generates multiple
    alerts rather than silently retrying forever with no visibility.
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

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(smtp_config["to_emails"])
    msg.attach(MIMEText(body, "plain"))

    for attempt_n in range(1, max_retries + 1):
        try:
            with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_config["username"], smtp_config["password"])
                server.sendmail(smtp_config["from_email"], smtp_config["to_emails"], msg.as_string())
            log.info(f"Failure alert email sent for {system_name}.")
            return True
        except Exception as e:
            log.warning(f"Failure alert email attempt {attempt_n}/{max_retries} failed to send: {e}")
            if attempt_n == max_retries:
                log.error(f"Could not send failure alert email for {system_name}: {e}")
                return False
            import time
            time.sleep(3)
    return False


def send_final_report(result: MonitoringResult, smtp_config: dict,
                       pdf_path: str, excel_path: str, max_retries: int = 2) -> bool:
    subject = f"InfraBeatOps Report - {result.system}"
    html_body = _build_report_html(result)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = smtp_config["from_email"]
    msg["To"] = ", ".join(smtp_config["to_emails"])

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

    for attempt in range(1, max_retries + 1):
        try:
            with smtplib.SMTP(smtp_config["host"], smtp_config["port"], timeout=60) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_config["username"], smtp_config["password"])
                server.sendmail(smtp_config["from_email"], smtp_config["to_emails"], msg.as_string())
            log.info(f"Final report email sent to {smtp_config['to_emails']}")
            return True
        except Exception as e:
            log.warning(f"Email send attempt {attempt}/{max_retries} failed: {e}")
            if attempt == max_retries:
                log.error(f"Failed to send final report email after {max_retries} attempts: {e}")
                return False
            import time
            time.sleep(3)

    return False
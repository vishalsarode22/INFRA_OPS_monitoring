"""
Diagnostic alert for Basis consultants.

An alert that says "CPU 92%" tells the person on call nothing they can act on.
This builds the whole picture in one message: what fired, which component is
responsible, the evidence behind that attribution, what to do about it, and
the full system state including everything that could NOT be read.

WHERE EACH SECTION COMES FROM -- this split is the point of the module:

  Summary, culprit, why, solution   -> config/correlation_rules.yaml
                                       Deterministic. Same input, same output,
                                       reviewable in a text file, defensible in
                                       a post-incident review.

  Narrative paragraph               -> Gemini, via evaluation/ai_analyzer
                                       Rewrites the summary ONLY. It cannot add
                                       a finding, change a severity, name a
                                       different culprit or invent a step.
                                       Rendered under a [HYPOTHESIS] banner.

The reason for that split is in the project's own history: an early version let
the model attach an SMLG hypothesis to an unrelated disk warning. A consultant
acting on an invented root cause at 3am is worse than an alert with no analysis
at all, because it costs them the hour they would have spent looking properly.

UNKNOWN metrics are listed in their own block and never counted as healthy.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Optional

from core.models import MonitoringResult, MetricResult, Status

try:
    from core.correlation import load_correlation_config
except Exception:                                    # pragma: no cover
    load_correlation_config = None                   # type: ignore


_SEVERITY_COLOUR = {
    "CRITICAL": "#b4232a",
    "WARNING": "#9a6700",
    "NORMAL": "#1a7f37",
    "UNKNOWN": "#57606a",
}

_STATUS_CHIP = {
    Status.CRITICAL: ("#b4232a", "#fbe9ea"),
    Status.WARNING: ("#9a6700", "#fff8e1"),
    Status.NORMAL: ("#1a7f37", "#e9f7ee"),
    Status.UNKNOWN: ("#57606a", "#eef0f2"),
}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _rule_config(rule_id: str) -> dict:
    if not rule_id or load_correlation_config is None:
        return {}
    try:
        return load_correlation_config().get(rule_id, {}) or {}
    except Exception:
        return {}


def _incident_rule_id(incident: Any) -> str:
    for attr in ("rule_id", "ruleId", "id", "correlation_rule"):
        value = getattr(incident, attr, None)
        if value:
            return str(value)
    if isinstance(incident, dict):
        for key in ("rule_id", "id"):
            if incident.get(key):
                return str(incident[key])
    return ""


def _incident_evidence(incident: Any) -> list[str]:
    value = getattr(incident, "evidence", None)
    if value is None and isinstance(incident, dict):
        value = incident.get("evidence")
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _section(title: str, body: str, accent: str = "#d0d7de") -> str:
    return f"""
    <div style="margin:0 0 18px 0;border:1px solid #d0d7de;border-left:4px solid {accent};
                border-radius:5px;background:#ffffff;">
      <div style="padding:9px 14px;border-bottom:1px solid #eaeef2;font-size:11px;
                  font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#57606a;">
        {_e(title)}
      </div>
      <div style="padding:13px 14px;font-size:13.5px;line-height:1.55;color:#1f2328;">
        {body}
      </div>
    </div>"""


def _metric_rows(metrics: list[MetricResult]) -> str:
    rows = []
    for m in metrics:
        colour, background = _STATUS_CHIP.get(m.status, _STATUS_CHIP[Status.UNKNOWN])
        source = m.source or "—"
        age = ""
        if m.freshness_seconds and m.freshness_seconds > 90:
            age = f" · {int(m.freshness_seconds // 60)}m old"
        rows.append(f"""
        <tr>
          <td style="padding:6px 9px;border-bottom:1px solid #eaeef2;font-family:Consolas,monospace;
                     font-size:12px;">{_e(m.name)}</td>
          <td style="padding:6px 9px;border-bottom:1px solid #eaeef2;font-family:Consolas,monospace;
                     font-size:12px;font-weight:700;">{_e(m.display_value)}</td>
          <td style="padding:6px 9px;border-bottom:1px solid #eaeef2;">
            <span style="background:{background};color:{colour};padding:2px 7px;border-radius:9px;
                         font-size:10.5px;font-weight:700;">{_e(m.status.value)}</span>
          </td>
          <td style="padding:6px 9px;border-bottom:1px solid #eaeef2;font-size:11.5px;color:#57606a;">
            {_e(source)}{_e(age)}
          </td>
          <td style="padding:6px 9px;border-bottom:1px solid #eaeef2;font-size:11.5px;color:#57606a;">
            {_e(m.detail or "")}
          </td>
        </tr>""")
    if not rows:
        return "<p style='color:#57606a;margin:0;'>No metrics were collected this cycle.</p>"
    return f"""
      <table style="width:100%;border-collapse:collapse;">
        <tr style="background:#f6f8fa;">
          <th style="text-align:left;padding:6px 9px;font-size:10.5px;text-transform:uppercase;
                     letter-spacing:.05em;color:#57606a;">Metric</th>
          <th style="text-align:left;padding:6px 9px;font-size:10.5px;text-transform:uppercase;
                     letter-spacing:.05em;color:#57606a;">Value</th>
          <th style="text-align:left;padding:6px 9px;font-size:10.5px;text-transform:uppercase;
                     letter-spacing:.05em;color:#57606a;">Status</th>
          <th style="text-align:left;padding:6px 9px;font-size:10.5px;text-transform:uppercase;
                     letter-spacing:.05em;color:#57606a;">Source</th>
          <th style="text-align:left;padding:6px 9px;font-size:10.5px;text-transform:uppercase;
                     letter-spacing:.05em;color:#57606a;">Detail</th>
        </tr>
        {''.join(rows)}
      </table>"""


def _bullets(items: list[str], mono: bool = False) -> str:
    if not items:
        return ""
    font = "font-family:Consolas,monospace;font-size:12px;" if mono else ""
    return ("<ul style='margin:0;padding-left:19px;'>"
            + "".join(f"<li style='margin-bottom:5px;{font}'>{_e(i)}</li>" for i in items)
            + "</ul>")


def build_diagnostic_alert(
    result: MonitoringResult,
    incident: Any = None,
    ai_analysis: Optional[Any] = None,
) -> tuple[str, str]:
    """
    Returns (subject, html_body).

    `incident` is the correlated incident this alert is about, if one was
    raised. Without it the message still reports full system health and says
    plainly that no correlation rule matched -- which is itself information:
    something breached a threshold that no rule explains.
    """
    severity = str(getattr(result.overall_status, "value", result.overall_status))
    accent = _SEVERITY_COLOUR.get(severity, "#57606a")
    stamp = result.cycle_timestamp.strftime("%Y-%m-%d %H:%M:%S")

    critical = [m for m in result.metrics if m.status is Status.CRITICAL]
    warning = [m for m in result.metrics if m.status is Status.WARNING]
    unknown = [m for m in result.metrics if m.status is Status.UNKNOWN]
    normal = [m for m in result.metrics if m.status is Status.NORMAL]

    rule_id = _incident_rule_id(incident) if incident is not None else ""
    cfg = _rule_config(rule_id)
    culprit = cfg.get("culprit") or {}

    subject = (f"[{severity}] InfraBeatOps · {result.system}/{result.client} · "
               f"{cfg.get('title') or 'System health alert'}")

    # ---------------------------------------------------------------- summary
    headline = []
    if critical:
        headline.append(f"{len(critical)} critical")
    if warning:
        headline.append(f"{len(warning)} warning")
    if unknown:
        headline.append(f"{len(unknown)} unreadable")
    if normal:
        headline.append(f"{len(normal)} normal")

    summary_body = f"""
      <p style="margin:0 0 9px 0;"><strong>{_e(result.system)}</strong>
        (client {_e(result.client)}) is <strong style="color:{accent};">{_e(severity)}</strong>
        as of {_e(stamp)}.</p>
      <p style="margin:0 0 9px 0;">Checks: {_e(' · '.join(headline) or 'none')}.</p>
      {'<p style="margin:0;">Correlated incident: <strong>' + _e(cfg.get('title') or rule_id) + '</strong>'
       + (f" (rule <code>{_e(rule_id)}</code>, confidence {_e(cfg.get('confidence'))})" if rule_id else "")
       + '.</p>'
       if rule_id else
       '<p style="margin:0;color:#9a6700;">No correlation rule matched these readings. '
       'A threshold breached that no rule explains -- treat the metric table below as the '
       'primary evidence and consider whether a rule is missing.</p>'}
    """

    sections = [_section("Summary", summary_body, accent)]

    # ---------------------------------------------------------------- culprit
    if culprit:
        confirm = culprit.get("confirm_in") or []
        note = culprit.get("confirm_notes")
        culprit_body = f"""
          <p style="margin:0 0 9px 0;font-size:15px;">
            <strong>{_e(culprit.get('component'))}</strong></p>
          <p style="margin:0 0 11px 0;">{_e(culprit.get('statement'))}</p>
          {'<p style="margin:0 0 6px 0;font-weight:600;">Confirm before acting:</p>' + _bullets(confirm) if confirm else ''}
          {f'<p style="margin:9px 0 0 0;padding:8px 10px;background:#fff8e1;border-radius:4px;font-size:12.5px;">{_e(note)}</p>' if note else ''}
        """
        sections.append(_section("Likely culprit", culprit_body, accent))

    # -------------------------------------------------------------------- why
    evidence = _incident_evidence(incident)
    breaching = critical + warning
    why_body = ""
    if breaching:
        why_body += "<p style='margin:0 0 6px 0;font-weight:600;'>Readings that breached threshold:</p>"
        why_body += _bullets(
            [f"{m.name} = {m.display_value}"
             + (f" (warning at {m.threshold_warning})" if m.threshold_warning is not None else "")
             + (f" (critical at {m.threshold_critical})" if m.threshold_critical is not None else "")
             + (f" — {m.detail}" if m.detail else "")
             for m in breaching], mono=True)
    if evidence:
        why_body += "<p style='margin:12px 0 6px 0;font-weight:600;'>Correlation evidence:</p>"
        why_body += _bullets(evidence, mono=True)
    if cfg.get("description"):
        why_body += (f"<p style='margin:12px 0 0 0;color:#57606a;'>Rule rationale: "
                     f"{_e(cfg['description'])}</p>")
    if why_body:
        sections.append(_section("Why this happened", why_body, accent))

    # --------------------------------------------------------------- solution
    remediation = cfg.get("remediation") or []
    if remediation:
        steps = "<ol style='margin:0;padding-left:20px;'>" + "".join(
            f"<li style='margin-bottom:6px;'>{_e(step)}</li>" for step in remediation
        ) + "</ol>"
        sections.append(_section("What to do", steps, accent))

    # ----------------------------------------------------------- unknown data
    if unknown:
        unknown_body = (
            "<p style='margin:0 0 8px 0;'>These could not be read this cycle. They are "
            "<strong>not</strong> counted as healthy, and any of them could be hiding the "
            "actual fault:</p>"
            + _bullets([f"{m.name} — {m.source or 'no source'}"
                        + (f" — {m.detail}" if m.detail else "")
                        for m in unknown], mono=True))
        sections.append(_section(f"Could not be read ({len(unknown)})", unknown_body, "#57606a"))

    # ------------------------------------------------------------ full health
    sections.append(_section(f"Full system health ({len(result.metrics)} metrics)",
                             _metric_rows(result.metrics)))

    # ------------------------------------------------------------------ error
    if result.errors:
        sections.append(_section(f"Collector errors ({len(result.errors)})",
                                 _bullets([str(e) for e in result.errors], mono=True),
                                 "#9a6700"))

    # --------------------------------------------------------------------- AI
    analysis = ai_analysis or result.ai_analysis
    if analysis is not None:
        narrative = getattr(analysis, "likely_root_cause", "") or ""
        ai_body = f"""
          <div style="padding:7px 10px;margin:0 0 11px 0;background:#eef0f2;border-radius:4px;
                      font-size:11px;font-weight:700;letter-spacing:.04em;color:#57606a;">
            HYPOTHESIS · CYCLE-LEVEL · NOT A FINDING
          </div>
          <p style="margin:0 0 9px 0;">{_e(narrative)}</p>
          <p style="margin:0;font-size:12px;color:#57606a;">
            Written by the model from the deterministic findings above. It cannot add a
            finding, change a severity or name a different culprit. Where it disagrees with
            the sections above, the sections above are correct.
          </p>"""
        sections.append(_section("AI narrative", ai_body, "#57606a"))

    body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f4f7;
                   font-family:-apple-system,'Segoe UI',Arial,sans-serif;">
  <div style="max-width:820px;margin:0 auto;padding:22px 18px;">
    <div style="background:{accent};color:#ffffff;padding:15px 18px;border-radius:5px 5px 0 0;">
      <div style="font-size:11px;letter-spacing:.09em;text-transform:uppercase;opacity:.85;">
        InfraBeatOps · SAP Basis Intelligence
      </div>
      <div style="font-size:21px;font-weight:700;margin-top:3px;">
        {_e(result.system)} · {_e(severity)}
      </div>
      <div style="font-size:12.5px;opacity:.9;margin-top:2px;">
        {_e(cfg.get('title') or 'System health alert')} · {_e(stamp)}
      </div>
    </div>
    <div style="background:#f4f4f7;padding:18px 0 0 0;">
      {''.join(sections)}
    </div>
    <p style="font-size:11px;color:#8b949e;margin:6px 0 0 0;">
      Generated by InfraBeatOps. Findings and remediation come from
      config/correlation_rules.yaml. "Could not read" is never "healthy".
    </p>
  </div>
</body></html>"""

    return subject, body

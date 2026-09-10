"""
Delivery routing.

One place that decides who gets told what, so the answer is the same wherever
the question is asked. Before this, email was sent from the pipeline and
WhatsApp would have been sent from somewhere else, and "why did nobody get
the alert" would have had two places to look.

TWO PATHS, DELIBERATELY DIFFERENT

deliver_profile_result()  -- a scheduled sweep finished. Goes to the channels
                             that profile names. This is routine reporting.

deliver_alert()           -- something is wrong. Routes by SEVERITY, not by
                             profile, and is suppressed if the same condition
                             already notified recently.

The separation matters. A CRITICAL finding must reach someone the moment it is
found, not when the next scheduled profile happens to deliver. And a routine
2-hourly summary must not use the same channel as an emergency, or the
emergency stops standing out.
"""

from __future__ import annotations

from datetime import datetime

from utils.logger import get_logger

log = get_logger(__name__, "delivery")


def _send_email(subject_result, smtp_config, incident=None, ai_analysis=None,
                diagnostic: bool = False) -> bool:
    try:
        if diagnostic:
            from notifications.email_report import send_diagnostic_alert
            return send_diagnostic_alert(subject_result, smtp_config,
                                         incident=incident,
                                         ai_analysis=ai_analysis)
        from notifications.email_report import send_final_report
        return send_final_report(subject_result, smtp_config)
    except Exception as exc:
        log.warning(f"Email delivery failed: {exc}")
        return False


def _send_whatsapp(system: str, evidence, summary: str) -> bool:
    try:
        from notifications.whatsapp import send_screenshots
        result = send_screenshots(system, evidence or [], summary=summary)
        if not result.get("sent"):
            log.info(f"WhatsApp not sent for {system}: "
                     f"{result.get('reason', 'see log')}")
        return bool(result.get("sent"))
    except Exception as exc:
        log.warning(f"WhatsApp delivery failed: {exc}")
        return False


def deliver_profile_result(profile: dict, result, smtp_config: dict,
                           evidence=None) -> dict:
    """
    Send a completed profile run to the channels that profile names.

    Returns a per-channel outcome rather than a single boolean: "the sweep was
    delivered" is not true or false when there are two channels and one of
    them failed.
    """
    channels = profile.get("deliver") or []
    if not channels:
        return {"delivered": {}, "note": "profile has no delivery channels"}

    system = getattr(result, "system", profile.get("system", "?"))
    status = getattr(getattr(result, "overall_status", None), "value", "")
    summary = f"{profile.get('label') or profile['id']} · {status}".strip(" ·")

    outcome = {}
    for channel in channels:
        if channel == "email":
            outcome["email"] = _send_email(result, smtp_config)
        elif channel == "whatsapp":
            outcome["whatsapp"] = _send_whatsapp(system, evidence, summary)

    log.info(f"Profile {profile['id']} delivered: {outcome}")
    return {"delivered": outcome, "profile": profile["id"],
            "channels": channels}


def deliver_alert(result, smtp_config: dict, incident=None,
                  ai_analysis=None, evidence=None,
                  now: datetime | None = None) -> dict:
    """
    Send an alert, routed by severity and suppressed if it repeats.

    The suppression key is system plus rule id -- not the timestamp -- so an
    ongoing incident is recognised across cycles instead of arriving as a new
    alert every sweep.
    """
    from core.profiles import channels_for, mark_alerted, should_alert

    system = getattr(result, "system", "?")
    severity = getattr(getattr(result, "overall_status", None), "value", "UNKNOWN")

    rule_id = ""
    if incident is not None:
        rule_id = (getattr(incident, "rule_id", "")
                   or (incident.get("rule_id", "") if isinstance(incident, dict) else ""))
    key = f"{system}:{rule_id or severity}"

    channels = channels_for(severity)
    if not channels:
        return {"sent": False, "reason": f"{severity} is not routed to any channel",
                "key": key}

    if not should_alert(key, severity, now):
        # Not an error. The condition is known and someone was told recently.
        return {"sent": False, "reason": "suppressed -- already alerted recently",
                "key": key, "severity": severity}

    outcome = {}
    for channel in channels:
        if channel == "email":
            outcome["email"] = _send_email(result, smtp_config, incident=incident,
                                           ai_analysis=ai_analysis, diagnostic=True)
        elif channel == "whatsapp":
            title = ""
            if incident is not None:
                title = (getattr(incident, "title", "")
                         or (incident.get("title", "") if isinstance(incident, dict) else ""))
            outcome["whatsapp"] = _send_whatsapp(
                system, evidence, f"{severity} · {title or 'system health alert'}")

    if any(outcome.values()):
        mark_alerted(key, now)

    log.info(f"Alert {key} [{severity}] delivered: {outcome}")
    return {"sent": any(outcome.values()), "delivered": outcome,
            "key": key, "severity": severity, "channels": channels}

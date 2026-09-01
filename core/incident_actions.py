"""Explicit operator actions for incident lifecycle management."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.incidents import Incident, IncidentStatus
from core.resolutions import ResolutionCategory

_ALLOWED = {"ACKNOWLEDGE", "INVESTIGATE", "RESOLVE"}


def _history(incident: Incident) -> list[dict[str, Any]]:
    history = getattr(incident, "action_history", None)
    if history is None:
        history = []
        incident.action_history = history
    return history


def _record(incident, action, actor, now, from_status, to_status, note=""):
    history = _history(incident)
    history.append({
        "action": action,
        "actor": actor.strip() or "operator",
        "timestamp": now.isoformat(),
        "from_status": from_status.value,
        "to_status": to_status.value,
        "note": note.strip(),
    })
    incident.action_history = history[-50:]


def acknowledge_incident(incident, *, actor="operator", now=None, note=""):
    now = now or datetime.now()
    if incident.status == IncidentStatus.RESOLVED:
        raise ValueError("A RESOLVED incident cannot be acknowledged.")
    if incident.status == IncidentStatus.ACKNOWLEDGED:
        return incident
    old = incident.status
    incident.status = IncidentStatus.ACKNOWLEDGED
    incident.acknowledged_at = now
    incident.acknowledged_by = actor.strip() or "operator"
    _record(incident, "ACKNOWLEDGE", actor, now, old, incident.status, note)
    return incident


def investigate_incident(incident, *, actor="operator", now=None, note=""):
    now = now or datetime.now()
    if incident.status == IncidentStatus.RESOLVED:
        raise ValueError("A RESOLVED incident cannot enter investigation.")
    if incident.status == IncidentStatus.INVESTIGATING:
        return incident
    old = incident.status
    incident.status = IncidentStatus.INVESTIGATING
    _record(incident, "INVESTIGATE", actor, now, old, incident.status, note)
    return incident


def resolve_incident(
    incident, *, summary, actor="operator",
    category=ResolutionCategory.UNKNOWN, actions_taken=None,
    confirmed_root_cause="", verified=False, now=None,
):
    now = now or datetime.now()
    if incident.status == IncidentStatus.RESOLVED:
        raise ValueError("Incident is already RESOLVED.")
    if not summary.strip():
        raise ValueError("Resolution summary is required.")
    old = incident.status
    incident.status = IncidentStatus.RESOLVED
    incident.resolved_at = now
    from core.resolutions import confirm_resolution
    confirm_resolution(
        incident, category=category, summary=summary,
        actions_taken=actions_taken, confirmed_root_cause=confirmed_root_cause,
        verified=verified, resolver=actor, resolved_at=now,
    )
    _record(incident, "RESOLVE", actor, now, old, incident.status, summary)
    return incident


def apply_incident_action(incident, action, **kwargs):
    action = str(action).strip().upper()
    if action not in _ALLOWED:
        raise ValueError(f"Unsupported incident action: {action}")
    if action == "ACKNOWLEDGE":
        return acknowledge_incident(incident, **kwargs)
    if action == "INVESTIGATE":
        return investigate_incident(incident, **kwargs)
    return resolve_incident(incident, **kwargs)

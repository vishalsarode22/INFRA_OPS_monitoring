"""Read-only API contract for InfraBeatOps intelligence.

This module is deliberately independent of Starlette TestClient. The dashboard
can include ``router`` while tests can exercise these route functions directly.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.status_snapshot import load_snapshot, list_snapshot_systems
from core.incident_store import load_incidents, save_incidents
from core.incident_actions import apply_incident_action
from core.resolutions import ResolutionCategory

router = APIRouter(prefix="/api", tags=["intelligence"])


def _snapshot_or_404(system_name: str) -> dict:
    snapshot = load_snapshot(system_name)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"System '{system_name}' snapshot not found.",
        )
    return snapshot


def _empty_intelligence() -> dict:
    return {
        "overall_signal": "NO_SIGNIFICANT_INTELLIGENCE",
        "score": 0.0,
        "signals": [],
        "key_findings": [],
        "limitations": [
            "Operational intelligence is not available in this snapshot."
        ],
    }


def system_health(system_name: str) -> dict:
    snapshot = _snapshot_or_404(system_name)
    return {
        "system": snapshot.get("system", system_name),
        "client": snapshot.get("client"),
        "cycle_timestamp": snapshot.get("cycle_timestamp"),
        "overall_status": snapshot.get("overall_status", "UNKNOWN"),
        "generated_at": snapshot.get("generated_at"),
        "available": True,
    }


def system_metrics(system_name: str) -> dict:
    snapshot = _snapshot_or_404(system_name)
    return {
        "system": snapshot.get("system", system_name),
        "client": snapshot.get("client"),
        "cycle_timestamp": snapshot.get("cycle_timestamp"),
        "metrics": snapshot.get("metrics", []),
    }


def system_incidents(system_name: str) -> dict:
    snapshot = _snapshot_or_404(system_name)
    return {
        "system": snapshot.get("system", system_name),
        "client": snapshot.get("client"),
        "cycle_timestamp": snapshot.get("cycle_timestamp"),
        "incidents": snapshot.get("incidents", []),
    }


def system_intelligence(system_name: str) -> dict:
    snapshot = _snapshot_or_404(system_name)
    return {
        "system": snapshot.get("system", system_name),
        "client": snapshot.get("client"),
        "cycle_timestamp": snapshot.get("cycle_timestamp"),
        "operational_intelligence": (
            snapshot.get("operational_intelligence") or _empty_intelligence()
        ),
    }


def system_overview(system_name: str) -> dict:
    snapshot = _snapshot_or_404(system_name)
    return {
        "system": snapshot.get("system", system_name),
        "client": snapshot.get("client"),
        "cycle_timestamp": snapshot.get("cycle_timestamp"),
        "overall_status": snapshot.get("overall_status", "UNKNOWN"),
        "metrics": snapshot.get("metrics", []),
        "incidents": snapshot.get("incidents", []),
        "events": snapshot.get("events", []),
        "operational_intelligence": (
            snapshot.get("operational_intelligence") or _empty_intelligence()
        ),
        "ai_analysis": snapshot.get("ai_analysis"),
        "generated_at": snapshot.get("generated_at"),
    }



class IncidentActionRequest(BaseModel):
    action: str
    actor: str = "operator"
    note: str = ""
    summary: str = ""
    category: ResolutionCategory = ResolutionCategory.UNKNOWN
    actions_taken: list[str] = Field(default_factory=list)
    confirmed_root_cause: str = ""
    verified: bool = False


def _find_persistent_incident(incident_id: str):
    for system_name in list_snapshot_systems():
        incidents = load_incidents(system_name)
        for incident in incidents:
            if str(incident.incident_id) == str(incident_id):
                return system_name, incidents, incident
    raise HTTPException(status_code=404, detail=f"Incident '{incident_id}' not found.")


def incident_action(incident_id: str, request: IncidentActionRequest) -> dict:
    system_name, incidents, incident = _find_persistent_incident(incident_id)
    action = request.action.strip().upper()
    kwargs = {"actor": request.actor, "note": request.note}
    if action == "RESOLVE":
        kwargs.pop("note", None)
        kwargs.update({
            "summary": request.summary,
            "category": request.category,
            "actions_taken": request.actions_taken,
            "confirmed_root_cause": request.confirmed_root_cause,
            "verified": request.verified,
        })
    try:
        apply_incident_action(incident, action, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    save_incidents(system_name, incidents)
    return {
        "success": True,
        "system": system_name,
        "incident": incident.to_dict(),
        "severity_authoritative": True,
        "action": action,
    }


def incident_rca(incident_id: str) -> dict:
    for system_name in list_snapshot_systems():
        snapshot = load_snapshot(system_name)
        if not snapshot:
            continue

        for incident in snapshot.get("incidents", []) or []:
            if str(incident.get("incident_id")) == str(incident_id):
                return {
                    "system": snapshot.get("system", system_name),
                    "client": snapshot.get("client"),
                    "incident": incident,
                    "ai_analysis": incident.get("ai_analysis"),
                    "operational_intelligence": snapshot.get(
                        "operational_intelligence"
                    ),
                }

    raise HTTPException(
        status_code=404,
        detail=f"Incident '{incident_id}' not found.",
    )


@router.get("/systems/{system_name}/health")
def api_system_health(system_name: str) -> dict:
    return system_health(system_name)


@router.get("/systems/{system_name}/metrics")
def api_system_metrics(system_name: str) -> dict:
    return system_metrics(system_name)


@router.get("/systems/{system_name}/incidents")
def api_system_incidents(system_name: str) -> dict:
    return system_incidents(system_name)


@router.get("/systems/{system_name}/intelligence")
def api_system_intelligence(system_name: str) -> dict:
    return system_intelligence(system_name)


@router.get("/systems/{system_name}/overview")
def api_system_overview(system_name: str) -> dict:
    return system_overview(system_name)


@router.get("/incidents/{incident_id}/rca")
def api_incident_rca(incident_id: str) -> dict:
    return incident_rca(incident_id)



@router.post("/incidents/{incident_id}/action")
def api_incident_action(incident_id: str, request: IncidentActionRequest) -> dict:
    return incident_action(incident_id, request)

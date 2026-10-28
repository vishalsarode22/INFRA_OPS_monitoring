"""JSON-backed incident persistence used until the database milestone."""

from __future__ import annotations

import json
import os
from typing import Iterable

from core.incidents import Incident
from utils.paths import BASE_DIR


INCIDENT_DIR = os.path.join(BASE_DIR, "dashboard", "incidents")


def _path(system: str) -> str:
    os.makedirs(INCIDENT_DIR, exist_ok=True)
    safe = "".join(c for c in system if c.isalnum() or c in "-_") or "system"
    return os.path.join(INCIDENT_DIR, f"{safe}.json")


def load_incidents(system: str) -> list[Incident]:
    path = _path(system)
    try:
        from core.atomic_json import read_json
        data = read_json(path, default=None)
        if not data:
            return []
        return [Incident.from_dict(item) for item in data.get("incidents", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_incidents(system: str, incidents: Iterable[Incident]) -> str:
    path = _path(system)
    payload = {"system": system, "incidents": [incident.to_dict() for incident in incidents]}
    from core.atomic_json import write_json
    write_json(path, payload)
    return path

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
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [Incident.from_dict(item) for item in data.get("incidents", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_incidents(system: str, incidents: Iterable[Incident]) -> str:
    path = _path(system)
    payload = {"system": system, "incidents": [incident.to_dict() for incident in incidents]}
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temp_path, path)
    return path

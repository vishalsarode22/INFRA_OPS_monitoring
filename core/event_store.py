"""Small JSON-backed persistence layer for monitoring events.

This is intentionally an intermediate store for Milestone 2. It gives the
scheduler a durable event lifecycle without introducing a database migration
before the event contract has stabilized. PostgreSQL will replace this store
in the persistence milestone while preserving the same engine contract.
"""

import json
import os
from typing import Iterable

from core.events import MonitoringEvent
from utils.paths import BASE_DIR


EVENT_DIR = os.path.join(BASE_DIR, "dashboard", "events")


def _path(system: str) -> str:
    os.makedirs(EVENT_DIR, exist_ok=True)
    safe = "".join(c for c in system if c.isalnum() or c in "-_") or "system"
    return os.path.join(EVENT_DIR, f"{safe}.json")


def load_events(system: str) -> list[MonitoringEvent]:
    path = _path(system)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return [MonitoringEvent.from_dict(item) for item in data.get("events", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_events(system: str, events: Iterable[MonitoringEvent]) -> str:
    path = _path(system)
    payload = {
        "system": system,
        "events": [event.to_dict() for event in events],
    }
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temp_path, path)
    return path

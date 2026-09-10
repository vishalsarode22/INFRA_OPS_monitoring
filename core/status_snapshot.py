"""
Writes/reads a JSON snapshot per system, so the dashboard can display
multiple systems independently.
"""

import copy
import json
import os
import threading
from datetime import datetime

from core.models import MonitoringResult
from utils.logger import get_logger
from utils.paths import BASE_DIR

log = get_logger(__name__, "application")

SNAPSHOT_DIR = os.path.join(BASE_DIR, "dashboard", "snapshots")


def _snapshot_path(system_name: str) -> str:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    safe_name = "".join(c for c in system_name if c.isalnum() or c in "-_")
    return os.path.join(SNAPSHOT_DIR, f"{safe_name}.json")


def save_snapshot(result: MonitoringResult, gui_results: list = None, system_name: str = None):
    name = system_name or result.system
    path = _snapshot_path(name)

    # Read the previous snapshot before overwriting it, so evidence from an
    # earlier successful cycle can be carried forward when this one collected
    # none. See the gui_evidence block below.
    previous = load_snapshot(name) or {}

    data = {
        "system": result.system,
        "client": result.client,
        "cycle_timestamp": result.cycle_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "overall_status": result.overall_status.value,
        "metrics": [
            {"name": m.name, "display_value": m.display_value,
             "status": m.status.value, "detail": m.detail}
            for m in result.metrics
        ],
        "ai_analysis": None,
        "operational_intelligence": None,
        "events": [e.to_dict() for e in result.events],
        "incidents": [i.to_dict() for i in result.incidents],
        "gui_evidence": [],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if result.operational_intelligence is not None:
        oi = result.operational_intelligence
        data["operational_intelligence"] = {
            "overall_signal": oi.overall_signal,
            "score": oi.score,
            "signals": [
                {
                    "category": item.category,
                    "metric": item.metric,
                    "strength": item.strength,
                    "summary": item.summary,
                }
                for item in oi.signals
            ],
            "key_findings": list(oi.key_findings),
            "limitations": list(oi.limitations),
        }

    if result.ai_analysis and result.ai_analysis.raw_response:
        ai = result.ai_analysis
        data["ai_analysis"] = {
            "severity": ai.severity, "root_cause": ai.likely_root_cause,
            "confidence": ai.confidence,
        }

    if gui_results:
        data["gui_evidence"] = [
            {"tcode": m.tcode, "display_value": m.display_value,
             "extra_data": m.extra_data, "stale": False}
            for m in gui_results
        ]
    else:
        # CARRY FORWARD rather than erase.
        #
        # SAP GUI evidence is expensive and fragile: it needs a Windows host
        # with an unlocked desktop, and it loses the race whenever another
        # window steals focus. A sweep where GUI failed used to overwrite a
        # snapshot that HAD evidence with one that had none -- so a single
        # bad run destroyed the last good screenshots and T-code readings.
        #
        # Metrics are NOT carried forward: those are live readings, and a
        # stale one shown as current is exactly the lie this project exists
        # to remove. Evidence is different -- it records something that
        # happened at a known time, so keeping it is honest provided it is
        # labelled with that time and marked stale.
        carried = previous.get("gui_evidence") or []
        if carried:
            captured = (previous.get("gui_evidence_captured_at")
                        or previous.get("cycle_timestamp")
                        or previous.get("generated_at"))
            data["gui_evidence"] = [
                {**item, "stale": True, "captured_at": captured} for item in carried
            ]
            data["gui_evidence_stale"] = True
            data["gui_evidence_captured_at"] = captured
            log.info(
                f"{name}: no GUI evidence this cycle; carrying forward "
                f"{len(carried)} record(s) from {captured}, marked stale."
            )

    # The AI analysis is derived from evidence, so a cycle that collected none
    # should not blank the last real analysis either.
    if data.get("ai_analysis") is None and previous.get("ai_analysis"):
        data["ai_analysis"] = {**previous["ai_analysis"], "stale": True}

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(f"Status snapshot saved for {name}: {path}")
    except Exception as e:
        log.error(f"Failed to save status snapshot for {name}: {e}")


_snapshot_cache: dict[str, tuple[float, int, dict]] = {}
_snapshot_cache_lock = threading.Lock()


def load_snapshot(system_name: str) -> dict:
    """
    The last written snapshot for a system, or None.

    Cached against (mtime, size). /api/overview calls this once per system on
    every poll and PRD.json alone is 112 KB, so the overview endpoint was
    parsing roughly a quarter of a megabyte of JSON per request -- multiplied
    by every open tab. Snapshots change once per sweep, i.e. every couple of
    hours, so re-parsing them at polling rate bought nothing.

    A dict is returned to callers that will mutate it (get_status splats it
    into a response, save_snapshot reads the previous one and builds on it),
    so hand out a copy rather than the cached object.
    """
    path = _snapshot_path(system_name)
    try:
        stat = os.stat(path)
    except OSError:
        with _snapshot_cache_lock:
            _snapshot_cache.pop(system_name, None)
        return None

    with _snapshot_cache_lock:
        hit = _snapshot_cache.get(system_name)
    if hit is not None and hit[0] == stat.st_mtime and hit[1] == stat.st_size:
        return copy.deepcopy(hit[2])

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    with _snapshot_cache_lock:
        _snapshot_cache[system_name] = (stat.st_mtime, stat.st_size, data)
    return copy.deepcopy(data)


def list_snapshot_systems() -> list[str]:
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    return [f[:-5] for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")]
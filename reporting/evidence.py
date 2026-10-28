"""Evidence identity and execution-history helpers for SAP GUI monitoring."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import uuid


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(value: object, fallback: str = "unknown") -> str:
    text = str(value or "").strip()
    text = _SAFE.sub("_", text)
    return text[:80] or fallback


def create_evidence_id(system: str, tcode: str, when=None) -> str:
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%d-%H%M%S")
    return f"EV-{safe_component(system).upper()}-{safe_component(tcode).upper()}-{stamp}-{uuid.uuid4().hex[:6].upper()}"


@dataclass
class EvidenceRecord:
    evidence_id: str
    system: str
    client: str
    tcode: str
    started_at: str
    finished_at: str | None = None
    status: str = "PENDING"
    attempts: int = 0
    recovery_actions: list[str] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)
    extracted_data: dict = field(default_factory=dict)
    incident_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "system": self.system,
            "client": self.client,
            "tcode": self.tcode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "attempts": self.attempts,
            "recovery_actions": list(self.recovery_actions),
            "screenshot_paths": list(self.screenshot_paths),
            "extracted_data": dict(self.extracted_data),
            "incident_id": self.incident_id,
        }


def evidence_directory(root: str | Path, system: str, tcode: str) -> Path:
    path = Path(root) / safe_component(system) / safe_component(tcode)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_evidence_index(records: list[EvidenceRecord], path: str | Path) -> None:
    # Append/upsert execution evidence instead of replacing history.
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                existing = payload
        except (OSError, json.JSONDecodeError):
            existing = []

    merged = {}
    order = []

    for item in existing:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        merged[evidence_id] = item
        order.append(evidence_id)

    for record in records:
        item = record.to_dict()
        evidence_id = str(item.get("evidence_id", "")).strip()
        if not evidence_id:
            continue
        if evidence_id not in merged:
            order.append(evidence_id)
        merged[evidence_id] = item

    target.write_text(
        json.dumps([merged[eid] for eid in order], indent=2, default=str),
        encoding="utf-8",
    )

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "reporting" / "evidence.py"

if not TARGET.exists():
    raise SystemExit(f"Missing {TARGET}")

backup = TARGET.with_suffix(TARGET.suffix + ".pre_8_11.bak")
if not backup.exists():
    shutil.copy2(TARGET, backup)

s = TARGET.read_text(encoding="utf-8")

old = """def write_evidence_index(records: list[EvidenceRecord], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([r.to_dict() for r in records], indent=2, default=str),
        encoding="utf-8",
    )
"""

new = """def write_evidence_index(records: list[EvidenceRecord], path: str | Path) -> None:
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
"""

if old not in s:
    raise SystemExit("Expected write_evidence_index implementation not found")
TARGET.write_text(s.replace(old, new), encoding="utf-8")
print("8.11 evidence index append/upsert repair applied.")

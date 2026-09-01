import json
from pathlib import Path
from reporting.evidence import EvidenceRecord, write_evidence_index

def make_record(eid, tcode):
    return EvidenceRecord(
        evidence_id=eid, system="TST", client="000", tcode=tcode,
        started_at="2026-08-19T10:00:00",
        finished_at="2026-08-19T10:01:00",
        status="captured (1)", attempts=1,
    )

def test_evidence_index_appends_without_erasing(tmp_path):
    path = tmp_path / "evidence_index.json"
    write_evidence_index([make_record("EV-1", "AL08")], path)
    write_evidence_index([make_record("EV-2", "SM51")], path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [x["evidence_id"] for x in payload] == ["EV-1", "EV-2"]

def test_evidence_index_upserts_same_id(tmp_path):
    path = tmp_path / "evidence_index.json"
    write_evidence_index([make_record("EV-1", "AL08")], path)
    updated = make_record("EV-1", "AL08")
    updated.attempts = 2
    write_evidence_index([updated], path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["attempts"] == 2

def test_rebuild_script_exists():
    assert Path("scripts/rebuild_evidence_index.py").exists()

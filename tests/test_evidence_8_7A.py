from datetime import datetime, timezone
from reporting.evidence import (
    EvidenceRecord, create_evidence_id, evidence_directory, safe_component,
)


def test_evidence_id_contains_system_and_tcode():
    value = create_evidence_id("TST", "SM12", datetime(2026, 8, 19, 9, 1, 2, tzinfo=timezone.utc))
    assert value.startswith("EV-TST-SM12-20260819-090102-")


def test_evidence_id_is_unique():
    a = create_evidence_id("TST", "SM12")
    b = create_evidence_id("TST", "SM12")
    assert a != b


def test_safe_component_removes_path_separators():
    assert "/" not in safe_component("TST/SM12")
    assert "\\" not in safe_component("TST\\SM12")


def test_evidence_directory_is_system_and_tcode_scoped(tmp_path):
    path = evidence_directory(tmp_path, "TST", "SM12")
    assert path == tmp_path / "TST" / "SM12"
    assert path.is_dir()


def test_evidence_record_serializes_recovery_and_screenshots():
    record = EvidenceRecord(
        evidence_id="EV-TST-SM12-TEST",
        system="TST",
        client="000",
        tcode="SM12",
        started_at="2026-08-19T09:00:00Z",
        status="RECOVERED",
        attempts=2,
        recovery_actions=["session_reconnect"],
        screenshot_paths=["TST/SM12/attempt-1.png", "TST/SM12/attempt-2.png"],
        extracted_data={"locks": 7},
    )
    data = record.to_dict()
    assert data["attempts"] == 2
    assert data["status"] == "RECOVERED"
    assert len(data["screenshot_paths"]) == 2
    assert data["extracted_data"]["locks"] == 7

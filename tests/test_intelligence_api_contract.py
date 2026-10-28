import dashboard.intelligence_api as intelligence_api


def snap():
    return {
        "system": "API-SYS", "client": "100",
        "cycle_timestamp": "2026-08-18 15:00:00",
        "overall_status": "WARNING",
        "metrics": [{"name": "sap.sm12.lock_count", "display_value": "23", "status": "WARNING"}],
        "events": [],
        "incidents": [{
            "incident_id": "INC-API-1", "rule_id": "LOCK",
            "severity": "WARNING", "status": "ACTIVE",
            "ai_analysis": {"root_cause_category": "LOCK_CONTENTION"},
        }],
        "operational_intelligence": {
            "overall_signal": "HIGH", "score": 0.88,
            "signals": [{"category": "BASELINE", "metric": "locks",
                         "strength": 0.9, "summary": "locks elevated"}],
            "key_findings": ["locks are above baseline"], "limitations": [],
        },
        "ai_analysis": {
            "severity": "WARNING", "root_cause": "lock contention",
            "confidence": "HIGH",
        },
        "generated_at": "2026-08-18 15:01:00",
    }


def patch_snapshot_loader(monkeypatch, value):
    monkeypatch.setattr(intelligence_api, "load_snapshot", lambda name: value)


def test_intelligence_endpoint_contract(monkeypatch):
    patch_snapshot_loader(monkeypatch, snap())
    payload = intelligence_api.system_intelligence("API-SYS")
    assert payload["operational_intelligence"]["overall_signal"] == "HIGH"
    assert payload["operational_intelligence"]["score"] == 0.88


def test_overview_is_single_dashboard_payload(monkeypatch):
    patch_snapshot_loader(monkeypatch, snap())
    payload = intelligence_api.system_overview("API-SYS")
    assert payload["overall_status"] == "WARNING"
    assert payload["incidents"][0]["incident_id"] == "INC-API-1"
    assert payload["operational_intelligence"]["overall_signal"] == "HIGH"


def test_health_endpoint_contract(monkeypatch):
    patch_snapshot_loader(monkeypatch, snap())
    payload = intelligence_api.system_health("API-SYS")
    assert payload["system"] == "API-SYS"
    assert payload["available"] is True
    assert payload["overall_status"] == "WARNING"


def test_missing_system_is_404(monkeypatch):
    from fastapi import HTTPException
    patch_snapshot_loader(monkeypatch, None)
    try:
        intelligence_api.system_intelligence("MISSING")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected HTTPException(404)")


def test_incident_rca_endpoint(monkeypatch):
    monkeypatch.setattr(intelligence_api, "list_snapshot_systems", lambda: ["API-SYS"])
    patch_snapshot_loader(monkeypatch, snap())
    payload = intelligence_api.incident_rca("INC-API-1")
    assert payload["incident"]["incident_id"] == "INC-API-1"
    assert payload["ai_analysis"]["root_cause_category"] == "LOCK_CONTENTION"
    assert payload["operational_intelligence"]["overall_signal"] == "HIGH"


def test_incident_rca_missing_is_404(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(intelligence_api, "list_snapshot_systems", lambda: ["API-SYS"])
    patch_snapshot_loader(monkeypatch, snap())
    try:
        intelligence_api.incident_rca("DOES-NOT-EXIST")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected HTTPException(404)")


def test_health_endpoint_does_not_expose_credentials(monkeypatch):
    patch_snapshot_loader(monkeypatch, snap())
    payload = intelligence_api.system_health("API-SYS")
    assert "password" not in str(payload).lower()
    assert "secret" not in str(payload).lower()

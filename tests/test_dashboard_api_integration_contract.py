from pathlib import Path
from dashboard.app import app

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "dashboard" / "static" / "index.html"

def _api_paths():
    return set(app.openapi().get("paths", {}).keys())

def test_intelligence_router_is_mounted():
    paths = _api_paths()
    assert "/api/systems/{system_name}/overview" in paths
    assert "/api/systems/{system_name}/intelligence" in paths
    assert "/api/incidents/{incident_id}/rca" in paths

def test_dashboard_consumes_overview_contract():
    source = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "/api/systems/" in source and "/overview" in source
    assert "operational_intelligence" in source

def test_dashboard_does_not_use_legacy_status_for_system_cards():
    source = DASHBOARD_HTML.read_text(encoding="utf-8")
    assert '"/api/status/" + encodeURIComponent(s.name)' not in source
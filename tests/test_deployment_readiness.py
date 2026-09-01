from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_systemd_service_restarts_on_failure():
    text = (ROOT / "packaging/systemd/infrabeatops.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in text
    assert "RestartSec=10" in text

def test_systemd_service_is_unprivileged():
    text = (ROOT / "packaging/systemd/infrabeatops.service").read_text(encoding="utf-8")
    assert "User=infrabeatops" in text
    assert "Group=infrabeatops" in text

def test_windows_launcher_uses_uvicorn():
    text = (ROOT / "packaging/windows/start_infrabeatops.ps1").read_text(encoding="utf-8")
    assert "uvicorn dashboard.app:app" in text

def test_operations_scheduler_documentation_is_current():
    text = (ROOT / "packaging/OPERATIONS.md").read_text(encoding="utf-8")
    assert "60-second startup grace period" in text
    assert "every 15 minutes" in text

def test_deployment_does_not_claim_two_hour_scheduler():
    text = (ROOT / "packaging/DEPLOYMENT.md").read_text(encoding="utf-8").lower()
    assert "2-hour scheduler" not in text
    assert "15 minutes" in text

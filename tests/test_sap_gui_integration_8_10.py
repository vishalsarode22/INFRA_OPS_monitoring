from pathlib import Path
import ast


def test_integration_probe_does_not_automate_login():
    source = Path("sap_gui/integration_probe.py").read_text(encoding="utf-8").lower()
    assert "login(" not in source
    assert "launch_saplogon" not in source
    assert "kill_sap_processes" not in source


def test_integration_probe_validates_system_and_client():
    source = Path("sap_gui/integration_probe.py").read_text(encoding="utf-8")
    assert "SystemName" in source
    assert "Client" in source
    assert "expected_system" in source
    assert "expected_client" in source


def test_integration_probe_uses_production_collector():
    source = Path("sap_gui/integration_probe.py").read_text(encoding="utf-8")
    assert "collect_tcode_evidence(" in source
    assert 'system=system' in source
    assert 'client=client' in source


def test_integration_probe_rejects_unconfigured_tcode():
    source = Path("sap_gui/integration_probe.py").read_text(encoding="utf-8")
    assert "not configured in monitoring_tasks.yaml" in source


def test_integration_probe_has_no_secret_literals():
    source = Path("sap_gui/integration_probe.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            assert "password=" not in text
            assert "sap_password" not in text
            assert "ssh_password" not in text

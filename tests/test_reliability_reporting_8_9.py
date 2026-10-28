from pathlib import Path

def test_collector_uses_three_attempt_policy():
    source = Path("collectors/sap_gui_collector.py").read_text(encoding="utf-8")
    assert "TCODE_MAX_ATTEMPTS = DEFAULT_TCODE_POLICY.max_attemptS".lower() in source.lower()
    assert "range(1, TCODE_MAX_ATTEMPTS + 1)" in source
    assert "recovery_actions" in source

def test_collector_uses_system_scoped_screenshots():
    source = Path("collectors/sap_gui_collector.py").read_text(encoding="utf-8")
    assert "system_evidence_root(system)" in source
    assert 'output_dir=str(system_evidence_root(system) / "screenshots")' in source

def test_collector_persists_evidence_index():
    source = Path("collectors/sap_gui_collector.py").read_text(encoding="utf-8")
    assert "EvidenceRecord(" in source
    assert "write_evidence_index(" in source
    assert "evidence_index.json" in source

def test_main_passes_system_identity_to_collector():
    source = Path("main.py").read_text(encoding="utf-8")
    assert "collect_tcode_evidence(tasks, system=system_config.get('name', 'UNKNOWN')" in source

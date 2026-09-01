from datetime import datetime
from reporting.system_paths import (
    safe_system_name, system_report_dir, system_excel_path,
    system_pdf_path, system_template_path, system_evidence_root,
)

def test_system_paths_are_isolated(tmp_path, monkeypatch):
    import reporting.system_paths as sp
    monkeypatch.setattr(sp, "BASE_DIR", str(tmp_path))
    when = datetime(2026, 8, 19, 9, 0, 0)

    assert system_excel_path("TST", when).endswith("TST/TST_Monitoring.xlsx")
    assert system_pdf_path("TST", when).endswith("TST/TST_Monitoring_Report.pdf")
    assert system_template_path("TST", when).endswith("TST/TST_Monitoring_Sheet.xlsx")
    assert system_excel_path("QAS", when).endswith("QAS/QAS_Monitoring.xlsx")
    assert system_excel_path("TST", when) != system_excel_path("QAS", when)

def test_system_evidence_root_is_isolated(tmp_path, monkeypatch):
    import reporting.system_paths as sp
    monkeypatch.setattr(sp, "BASE_DIR", str(tmp_path))
    assert system_evidence_root("TST").name == "evidence"
    assert system_evidence_root("TST") != system_evidence_root("QAS")

def test_system_name_is_filesystem_safe():
    assert safe_system_name("172.16.1.22") == "172.16.1.22"
    assert "/" not in safe_system_name("PROD/01")

def test_main_uses_system_scoped_report_paths():
    source = open("main.py", encoding="utf-8").read()
    assert "system_pdf_path(" in source
    assert "system_excel_path(" in source
    assert "system_template_path(" in source

def test_collector_accepts_system_identity():
    source = open("collectors/sap_gui_collector.py", encoding="utf-8").read()
    assert 'system: str = "UNKNOWN"' in source
    assert "create_evidence_id(" in source
    assert "write_evidence_index(" in source

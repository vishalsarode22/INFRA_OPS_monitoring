from reporting.evidence import create_evidence_id, evidence_directory

def test_evidence_is_system_scoped(tmp_path):
    p = evidence_directory(tmp_path, "TST", "SM12")
    assert p == tmp_path / "TST" / "SM12"
    assert p.is_dir()

def test_evidence_id_is_system_and_tcode_specific():
    a = create_evidence_id("TST", "SM12")
    b = create_evidence_id("QAS", "SM12")
    assert a.startswith("EV-TST-SM12-")
    assert b.startswith("EV-QAS-SM12-")
    assert a != b

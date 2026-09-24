"""
Round 27: RCA detail fixes found in the PS4 RCA of 23.09.2026 11:43 --
capped lock counts, IBOPS's own session ranked as the top culprit, SAP icon
codes in tables, and raw milliseconds in the culprit list.
"""
import sap_gui.rca_actions as ra


def test_culprits_skip_the_monitoring_session_and_put_measured_findings_first():
    from evaluation.rca_pipeline import rules_based_analysis
    evidence = [
        {"tcode": "SM66", "facts": {"long_running": [
            {"wp": "58", "type": "DIA", "user": "PS4_ADMIN", "elapsed": "00:45:58",
             "program": "CL_SERVER_INFO================CP"},
            {"wp": "82", "type": "DIA", "user": "58327", "elapsed": "00:31:28",
             "program": "CL_MD_BP======================CP"}]}},
        {"tcode": "STAD", "facts": {"lookback_minutes": 30, "top_by_response": [
            {"user": "BATCHUSER", "steps": 1, "total_resp_ms": 10971124.0, "worst_ms": 10971124.0,
             "db_ms": 51677.0, "cpu_ms": 40900.0, "programs": ["ZSD_LS_RET_ITEMS_PRICE_API"]}]}},
    ]
    out = rules_based_analysis(evidence, {})
    names = [c["name"] for c in out["culprits"]]
    assert not any("CL_SERVER_INFO" in n for n in names), "IBOPS's own collection is not a culprit"
    assert any("58327" in n for n in names), "a real long-running dialog step still counts"
    assert names[0].startswith("BATCHUSER"), "the measured 3-hour step outranks a snapshot"
    assert "total 182 min 51 s" in out["culprits"][0]["evidence"], "milliseconds are formatted"
    assert out["severity"] == "CRITICAL"


class _Grid:
    RowCount = 1284


def test_lock_count_comes_from_the_screen_not_the_read_limit(monkeypatch):
    # PS4 read 500 rows of a 1,284-row lock table and reported "500".
    rows = [{"user": "58327", "table": "MARC", "arg": f"500000001000203998{i:03d}",
             "date": "", "time": ""} for i in range(500)]
    field = type("F", (), {"Text": ""})()
    monkeypatch.setattr(ra, "_field_by_label", lambda session, label: field)
    monkeypatch.setattr(ra, "_button_by_text", lambda *a, **k: None)
    monkeypatch.setattr(ra, "_f8", lambda session: None)
    monkeypatch.setattr(ra, "_enter", lambda session: None)
    monkeypatch.setattr(ra, "wait_until_not_busy", lambda session: None)
    monkeypatch.setattr(ra, "_grid", lambda session: _Grid())
    monkeypatch.setattr(ra, "_grid_headers", lambda grid: {})
    monkeypatch.setattr(ra, "_col", lambda h, *n: "C")
    monkeypatch.setattr(ra, "_rows", lambda grid, cols, limit=500: rows)
    monkeypatch.setattr(ra, "_statusbar", lambda session: "")
    monkeypatch.setattr(ra, "_screen_texts", lambda session: ["Lock Table (1284)"])

    class _Session:
        def findById(self, _id): raise Exception("not in this fake")

    out = ra._sm12_search(_Session(), "*", "*")
    assert out["lock_count"] == 1284
    assert out["rows_read"] == 500 and out["count_capped"] is True
    assert out["top_users"][0] == {"user": "58327", "locks": 500}


def test_icon_codes_do_not_reach_the_tables():
    assert ra._strip_icons("@5B\\Q@") == ""
    assert ra._strip_icons("@0A@ Availability") == "Availability"

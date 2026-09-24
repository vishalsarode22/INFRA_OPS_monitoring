"""
Round 26: the performance RCA fires from the sweep's own SMLG reading, and
its report states counts, units and a root cause correctly.
Figures: PS4 manual RCA of 23.09.2026 11:06.
"""
from datetime import datetime, timedelta

import pytest

from core.rca_trigger import TriggerConfig, sweep_decision


@pytest.fixture
def _state(tmp_path, monkeypatch):
    import core.rca_trigger as rt
    monkeypatch.setattr(rt, "RCA_STATE_PATH", tmp_path / "rca_last_fired.json")
    return rt


def test_sweep_fires_when_smlg_is_over_the_band(_state):
    cfg = TriggerConfig(threshold_ms=1500)
    fire, reason = sweep_decision("PS4", 2404, "vhrrnps4ci_PS4_00", cfg)
    assert fire and "SMLG 2404 ms on vhrrnps4ci_PS4_00 over 1500 ms" in reason


def test_sweep_does_not_fire_below_the_band_or_inside_the_cooldown(_state):
    cfg = TriggerConfig(threshold_ms=1500, cooldown_minutes=30)
    assert sweep_decision("PS4", 636, "ci", cfg) == (False, "636 ms below the 1500 ms threshold")
    _state.mark_fired_on_disk("PS4", "earlier run")
    fire, reason = sweep_decision("PS4", 2404, "ci", cfg)
    assert not fire and "cooldown" in reason
    # ...and again once the cooldown has passed
    fire, _ = sweep_decision("PS4", 2404, "ci", cfg, now=datetime.now() + timedelta(minutes=31))
    assert fire


def test_work_process_counts_come_from_the_screen_header():
    from sap_gui.rca_actions import _wp_summary
    # SM50 header on PS4: Dialog 60/55 and Background 20/15 -- 5 busy each.
    rows = [{"type": "DIA", "state": "Running"}] * 5 + [{"type": "BTC", "state": "On Hold"}] * 6
    loose = _wp_summary(rows)
    tight = _wp_summary(rows, header_counts={"DIA": (60, 55), "BTC": (20, 15)})
    # Counting rows says 6 BTC busy; SAP's own header says 5 of 20.
    assert "BTC: 6 of 6 busy" in loose["focus"][0]
    assert tight["focus"][0] == "Work processes: 80 configured, 10 in use (BTC: 5 of 20 busy, DIA: 5 of 60 busy)"
    assert tight["counts_source"] == "screen header"


def test_sap_icon_codes_are_stripped():
    from sap_gui.rca_actions import _strip_icons
    assert _strip_icons("@5B\\Q@") == ""
    assert _strip_icons("@0A@ High") == "High"


def test_root_cause_names_the_program_and_grades_it():
    from evaluation.rca_pipeline import _rules_verdict
    facts = {"STAD": {"top_by_response": [{"user": "64014", "programs": ["ZRP_SD_SALES_REGISER"],
                                           "total_resp_ms": 1784335.0, "worst_ms": 1784335.0,
                                           "db_ms": 342984.0, "cpu_ms": 1461040.0}]},
             "SM50": {"priv_count": 0}}
    severity, root_cause, lead = _rules_verdict(facts, ["culprit"])
    assert severity == "CRITICAL"          # a 29-minute step holds a work process
    assert root_cause.startswith("64014 ran ZRP_SD_SALES_REGISER as a single step of 29 min 44 s")
    assert "CPU-bound" in root_cause and "Profile the ABAP (SAT)" in lead["step"]
    assert _rules_verdict({"SM50": {"priv_count": 0}}, [])[0] == "NORMAL"

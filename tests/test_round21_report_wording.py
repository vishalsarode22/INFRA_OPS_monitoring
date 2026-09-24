"""
Round 21: no impossible lock-holder line, backup age in minutes, and an
honest AI section when no model ran. Figures: PS4 run of 22.09.2026 15:09.
"""
from datetime import datetime

from core.models import AIAnalysis, MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import _gui


def test_lock_holder_is_not_linked_when_rfc_and_screen_disagree():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.CRITICAL
    res.metrics = [MetricResult(name="sap.sm12.locks_per_user_max", value=358,
                                display_value="358 count", status=Status.CRITICAL,
                                detail="CPIUSER 358, SAP_WFRT 12, BGRFC_SUPER 4")]
    gui = [_gui("SM12", lock_count=166), _gui("SM66", process_rows=[])]
    da = deterministic_analysis(res, narrate_all(gui), gui)
    assert not any(i.startswith("[SM12 + SM66]") for i in da["incidents"])
    assert any(c.startswith("[SM12]") and "358" in c for c in da["conflicts"])


def test_recent_backup_age_is_given_in_minutes():
    from reporting.excel_template_writer import _build_observation
    db12 = _gui("DB12", finished_at="2026-09-22T15:10:55",
                latest_backup={"end_time": "22.09.2026 14:46:21", "status": "successful"})
    assert _build_observation(db12, "OK").startswith("Last successful backup is 25 min old")
    old = _gui("DB12", finished_at="2026-09-22T14:12:20",
               latest_backup={"end_time": "21.09.2026 14:47:58", "status": "successful"})
    assert _build_observation(old, "OK").startswith("Last successful backup is 23 h old")


def test_ai_placeholder_is_recognised_and_the_pdf_still_builds(tmp_path):
    from reporting.production_reports import _ai_unavailable, generate_system_pdf
    placeholder = AIAnalysis(severity="WARNING", root_cause_category="AI_UNAVAILABLE",
                             likely_root_cause=("The deterministic monitoring and correlation "
                                                "engines completed, but structured LLM analysis "
                                                "was unavailable."), confidence="LOW")
    real = AIAnalysis(severity="CRITICAL", likely_root_cause="Enqueue lock build-up.", confidence="MEDIUM")
    assert _ai_unavailable(placeholder) and not _ai_unavailable(real)
    res = MonitoringResult(system="PS4", client="500", cycle_timestamp=datetime(2026, 9, 22, 15, 9),
                           ai_analysis=placeholder)
    out = tmp_path / "r.pdf"
    generate_system_pdf(res, [_gui("SM13", update_count=0, failed_update_count=0)], str(out))
    assert out.stat().st_size > 1000

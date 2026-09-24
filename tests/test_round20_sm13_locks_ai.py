"""
Round 20: SM13 Initial vs failed, the SM12 lock holder linked to SM66, and
the AI given the report's own incidents and cross-checks.

Figures are the PS4 run of 22.09.2026 14:45.
"""
from datetime import datetime

from core.models import MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import _gui


def test_sm13_fresh_initial_updates_are_in_progress_not_failed():
    from sap_gui.tcode_actions import _sm13_classify
    now = datetime(2026, 9, 22, 14, 46, 48)
    fresh = [{"STATUS": "Initial", "DATUM": "22.09.2026", "ZEIT": "14:46:45"},
             {"STATUS": "Initial", "DATUM": "22.09.2026", "ZEIT": "14:45:32"}]
    assert _sm13_classify(fresh, now) == {"error": 0, "stale": 0, "pending": 2, "other": 0}
    old = [{"STATUS": "Initial", "DATUM": "22.09.2026", "ZEIT": "14:30:00"},
           {"STATUS": "Err", "DATUM": "22.09.2026", "ZEIT": "14:40:00"},
           {"STATUS": "V1 processed", "DATUM": "22.09.2026", "ZEIT": "14:46:00"}]
    assert _sm13_classify(old, now) == {"error": 1, "stale": 1, "pending": 0, "other": 1}


def test_sm13_grading_and_metric_follow_the_failed_count():
    from reporting.excel_template_writer import _status_for_metric, _build_observation
    from core.tcode_metrics import normalize_tcode_result
    in_progress = _gui("SM13", update_count=2, failed_update_count=0, error_update_count=0,
                       stale_initial_count=0, pending_update_count=2,
                       update_summary="2 update record(s): 2 in progress (Initial, under 10 min).")
    assert _status_for_metric(in_progress) == "OK"
    assert _build_observation(in_progress, "OK").startswith("Updates in progress")
    metric = [m for m in normalize_tcode_result(in_progress) if m.name == "sap.sm13.failed_updates"]
    assert metric and metric[0].value == 0, "the metric counts failed updates, not all updates"

    stale = _gui("SM13", update_count=1, failed_update_count=1, error_update_count=0,
                 stale_initial_count=1, pending_update_count=0)
    assert _status_for_metric(stale) == "WARNING"
    error = _gui("SM13", update_count=1, failed_update_count=1, error_update_count=1,
                 stale_initial_count=0, pending_update_count=0)
    assert _status_for_metric(error) == "CRITICAL"


def _ps4_1445():
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.CRITICAL
    res.metrics = [MetricResult(name="sap.sm12.locks_per_user_max", value=2063,
                                display_value="2063 count", status=Status.CRITICAL,
                                detail="10265 2063, SAP_WFRT 13, BGRFC_SUPER 5, 61511 2")]
    sm12 = _gui("SM12", lock_count=2211)
    sm66 = _gui("SM66", active_processes=28, running_processes=14, on_hold_processes=14,
                waiting_processes=160, priv_mode_processes=0, process_rows=[
                    {"SERVER_NAME": "vhrrnps4ci_PS4_00", "WP_INDEX": "50", "WP_TYPE_DISP": "DIA",
                     "STATE_DISP": "Running", "WP_PROGRAM": "SAPLJ1I4A", "USER_NAME": "10265"},
                    {"SERVER_NAME": "vhrrnps4ci_PS4_00", "WP_INDEX": "64", "WP_TYPE_DISP": "UPD",
                     "STATE_DISP": "Running", "WP_PROGRAM": "SAPLSCDB", "USER_NAME": "63428"}])
    return res, [sm12, sm66]


def test_top_lock_holder_is_linked_to_their_work_process():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    res, gui = _ps4_1445()
    da = deterministic_analysis(res, narrate_all(gui), gui)
    (note,) = [i for i in da["incidents"] if i.startswith("[SM12 + SM66]")]
    assert "User 10265 holds 2,063 lock entries of 2,211" in note
    assert "SAPLJ1I4A in DIA work process 50 on vhrrnps4ci_PS4_00 (Running)" in note


def test_lock_holder_without_a_work_process_is_not_called_orphaned():
    from reporting.check_narratives import narrate_all, deterministic_analysis
    res, gui = _ps4_1445()
    gui[1].extra_data["process_rows"] = []
    da = deterministic_analysis(res, narrate_all(gui), gui)
    (note,) = [i for i in da["incidents"] if i.startswith("[SM12 + SM66]")]
    assert "no active work process" in note and "orphan" not in note


def test_ai_prompt_carries_the_reports_incidents_and_cross_checks():
    from evaluation.ai_analyzer import _report_findings
    from evaluation.ai_context import build_rca_prompt
    res, gui = _ps4_1445()
    res.metrics.append(MetricResult(name="sap.sm58.stuck_entries", value=500,
                                    display_value="500 count", status=Status.CRITICAL))
    gui.append(_gui("SM58", trfc_status="CRITICAL", failed_entries=4,
                    information={"entries_displayed": 4, "failed_entries": 4}))
    findings = _report_findings(res, gui)
    assert any("10265" in i for i in findings["correlated_incidents"])
    assert any("at least 500" in c for c in findings["cross_checks"])
    prompt = build_rca_prompt({"system": "PS4", "client": "500", "report_findings": findings})
    assert "at least 500" in prompt and "10265" in prompt

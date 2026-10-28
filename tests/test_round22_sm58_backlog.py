"""
Round 22: the SM58 RFC figure split into today's failures (comparable with
the SM58 screen) and the older backlog (housekeeping, WARNING at most).
"""
from core.models import MetricResult, MonitoringResult, Status

try:
    from test_round18_reads_and_reports import _gui
except ImportError:  # tests/ collected as a package
    from tests.test_round18_reads_and_reports import _gui


def _rows():
    today = [["20260922", "WORKFLOW_LOCAL_500"]] * 5
    older = [["20260903", "WORKFLOW_LOCAL_500"]] * 580 + [["20260615", "CPI_S4"]] * 27
    return today + older


def test_today_and_backlog_are_separate_metrics():
    from collectors.rfc_collector import _sm58_sysfail_metrics
    today, backlog = _sm58_sysfail_metrics(_rows(), "20260922", 20000)
    assert today.name == "sap.sm58.stuck_entries" and today.value == 5
    assert today.extra_data["scope"] == "today"
    assert "607 older, oldest 15.06.2026" in today.detail
    assert backlog.name == "sap.sm58.sysfail_backlog" and backlog.value == 607
    assert backlog.status == Status.WARNING, "an old backlog is housekeeping, not CRITICAL"
    assert "WORKFLOW_LOCAL_500 585" in backlog.detail


def test_a_capped_read_says_at_least():
    from collectors.rfc_collector import _sm58_sysfail_metrics
    rows = [["20260901", "WORKFLOW_LOCAL_500"]] * 500
    (today, backlog) = _sm58_sysfail_metrics(rows, "20260922", 500)
    assert today.value == 0 and backlog.display_value == "500+ count"
    assert "at least this many" in backlog.detail


def test_no_cross_check_when_today_matches_the_screen():
    from collectors.rfc_collector import _sm58_sysfail_metrics
    from reporting.check_narratives import narrate_all, deterministic_analysis
    res = MonitoringResult(system="PS4", client="500")
    res.overall_status = Status.CRITICAL
    res.metrics = _sm58_sysfail_metrics(_rows(), "20260922", 20000)
    sm58 = _gui("SM58", trfc_status="CRITICAL", failed_entries=5,
                information={"entries_displayed": 5, "failed_entries": 5})
    da = deterministic_analysis(res, narrate_all([sm58]), [sm58])
    assert not any(c.startswith("[SM58]") for c in da["conflicts"])
